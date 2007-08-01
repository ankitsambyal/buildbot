
import os, sys
from itertools import count

from zope.interface import implements
from twisted.python import log
from twisted.application import strports, service
from twisted.web import server, distrib, static
from twisted.spread import pb

from buildbot.interfaces import IControl, IStatusReceiver
from buildbot.status.builder import SUCCESS, WARNINGS, FAILURE, EXCEPTION

from buildbot.status.web.base import HtmlResource
from buildbot.status.web.waterfall import WaterfallStatusResource
from buildbot.status.web.changes import ChangesResource
from buildbot.status.web.builder import BuildersResource

# this class contains the status services (WebStatus and the older Waterfall)
# which can be put in c['status']. It also contains some of the resources
# that are attached to the WebStatus at various well-known URLs, which the
# admin might wish to attach (using WebStatus.putChild) at other URLs.


class TimelineOfEverything(WaterfallStatusResource):

    def __init__(self):
        HtmlResource.__init__(self)

    def render(self, request):
        webstatus = request.site.webstatus
        self.css = webstatus.css
        self.status = request.site.status
        self.changemaster = webstatus.parent.change_svc
        self.categories = None
        self.title = self.status.getProjectName()
        if self.title is None:
            self.title = "BuildBot"
        return WaterfallStatusResource.render(self, request)


class LastBuild(HtmlResource):
    def body(self, request):
        return "missing\n"

def getLastNBuilds(status, numbuilds, desired_builder_names=None):
    """Return a list with the last few Builds, sorted by start time.
    builder_names=None means all builders
    """

    # TODO: this unsorts the list of builder names, ick
    builder_names = set(status.getBuilderNames())
    if desired_builder_names is not None:
        desired_builder_names = set(desired_builder_names)
        builder_names = builder_names.intersection(desired_builder_names)

    # to make sure that we get everything, we must get 'numbuilds' builds
    # from *each* source, then sort by ending time, then trim to the last
    # 20. We could be more efficient, but it would require the same
    # gnarly code that the Waterfall uses to generate one event at a
    # time.
    events = []
    for builder_name in builder_names:
        builder = status.getBuilder(builder_name)
        for build_number in count(1):
            if build_number > numbuilds:
                break # enough from this builder, move on to another
            build = builder.getBuild(-build_number)
            if not build:
                break # no more builds here, move on to the next builder
            #if not build.isFinished():
            #    continue
            (build_start, build_end) = build.getTimes()
            event = (build_start, builder_name, build)
            events.append(event)
    def _sorter(a, b):
        return cmp( a[:2], b[:2] )
    events.sort(_sorter)
    # now only return the actual build, and only return some of them
    return [e[2] for e in events[-numbuilds:]]


def oneLineForABuild(status, build):
    css_classes = {SUCCESS: "success",
                   WARNINGS: "warnings",
                   FAILURE: "failure",
                   EXCEPTION: "exception",
                   }

    builder_name = build.getBuilder().getName()
    results = build.getResults()
    rev = build.getProperty("got_revision")
    if len(rev) > 20:
        rev = "?"
    values = {'class': css_classes[results],
              'builder_name': builder_name,
              'buildnum': build.getNumber(),
              'results': css_classes[results],
              'buildurl': status.getURLForThing(build),
              'rev': rev,
              }
    fmt = ('<div class="%(class)s">Build '
           '<a href="%(buildurl)s">#%(buildnum)d</a> of '
           '%(builder_name)s [%(rev)s]: '
           '<span class="%(class)s">%(results)s</span></div>\n')
    data = fmt % values
    return data

# /_buildbot/one_line_per_build
class OneLinePerBuild(HtmlResource):
    """This shows one line per build, combining all builders together. Useful
    query arguments:

    numbuilds=: how many lines to display
    builder=: show only builds for this builder. Multiple builder= arguments
              can be used to see builds from any builder in the set.
    """

    def __init__(self, numbuilds=20):
        HtmlResource.__init__(self)
        self.numbuilds = numbuilds

    def getChild(self, path, request):
        status = request.site.status
        builder = status.getBuilder(path)
        return OneLinePerBuildOneBuilder(builder)

    def body(self, request):
        status = request.site.status
        numbuilds = self.numbuilds
        if "numbuilds" in request.args:
            numbuilds = int(request.args["numbuilds"][0])

        desired_builder_names = None
        if "builder" in request.args:
            desired_builder_names = request.args["builder"]
        builds = getLastNBuilds(status, numbuilds, desired_builder_names)
        data = ""
        for build in reversed(builds):
            data += oneLineForABuild(status, build)
        else:
            data += "<div>No matching builds found</div>"
        return data



# /_buildbot/one_line_per_build/$BUILDERNAME
class OneLinePerBuildOneBuilder(HtmlResource):
    def __init__(self, builder, numbuilds=20):
        HtmlResource.__init__(self)
        self.builder = builder
        self.numbuilds = numbuilds

    def body(self, request):
        status = request.site.status
        numbuilds = self.numbuilds
        if "numbuilds" in request.args:
            numbuilds = int(request.args["numbuilds"][0])
        # walk backwards through all builds of a single builder

        # islice is cool but not exactly what we need here
        #events = itertools.islice(b.eventGenerator(), self.numbuilds)

        css_classes = {SUCCESS: "success",
                       WARNINGS: "warnings",
                       FAILURE: "failure",
                       EXCEPTION: "exception",
                       }

        data = ""
        i = 1
        while i < numbuilds:
            build = self.builder.getBuild(-i)
            if not build:
                break
            i += 1

            data += oneLineForABuild(status, build)

        return data



HEADER = '''
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN"
 "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">

<html
 xmlns="http://www.w3.org/1999/xhtml"
 lang="en"
 xml:lang="en">
'''

HEAD_ELEMENTS = [
    '<title>%(title)s</title>',
    '<link href="%(root)sbuildbot.css" rel="stylesheet" type="text/css" />',
    ]
BODY_ATTRS = {
    'vlink': "#800080",
    }

FOOTER = '''
</html>
'''


class WebStatus(service.MultiService):
    implements(IStatusReceiver)
    # TODO: IStatusReceiver is really about things which subscribe to hear
    # about buildbot events. We need a different interface (perhaps a parent
    # of IStatusReceiver) for status targets that don't subscribe, like the
    # WebStatus class. buildbot.master.BuildMaster.loadConfig:737 asserts
    # that everything in c['status'] provides IStatusReceiver, but really it
    # should check that they provide IStatusTarget instead.

    """
    The webserver provided by this class has the following resources:

     /waterfall : the big time-oriented 'waterfall' display, with links
                  to individual changes, builders, builds, steps, and logs.
                  A number of query-arguments can be added to influence
                  the display.
     /builders/BUILDERNAME: a page summarizing the builder. This includes
                            references to the Schedulers that feed it,
                            any builds currently in the queue, which
                            buildslaves are designated or attached, and a
                            summary of the build process it uses.
     /builders/BUILDERNAME/builds/NUM: a page describing a single Build
     /builders/BUILDERNAME/builds/NUM/steps/STEPNAME: describes a single step
     /builders/BUILDERNAME/builds/NUM/steps/STEPNAME/logs/LOGNAME: a StatusLog
     /builders/BUILDERNAME/builds/NUM/tests : summarize test results
     /builders/BUILDERNAME/builds/NUM/tests/TEST.NAME: results of one test
     /changes : summarize all ChangeSources
     /changes/CHANGENUM: a page describing a single Change
     /schedulers/SCHEDULERNAME: a page describing a Scheduler, including
                                a description of its behavior, a list of the
                                Builders it triggers, and list of the Changes
                                that are queued awaiting the tree-stable
                                timer, and controls to accelerate the timer.
     /others...

    All URLs for pages which are not defined here are used to look for files
    in BASEDIR/public_html/ , which means that /robots.txt or /buildbot.css
    or /favicon.ico can be placed in that directory.

    If an index file (index.html, index.htm, or index, in that order) is
    present in public_html/, it will be used for the root resource. If not,
    the default behavior is to put a redirection to the /waterfall page.

    All of the resources provided by this service use relative URLs to reach
    each other. The only absolute links are the c['projectURL'] links at the
    top and bottom of the page, and the buildbot home-page link at the
    bottom.

    This webserver defines class attributes on elements so they can be styled
    with CSS stylesheets. All pages pull in public_html/buildbot.css, and you
    can cause additional stylesheets to be loaded by adding a suitable <link>
    to the WebStatus instance's .head_elements attribute.

    Buildbot uses some generic classes to identify the type of object, and
    some more specific classes for the various kinds of those types. It does
    this by specifying both in the class attributes where applicable,
    separated by a space. It is important that in your CSS you declare the
    more generic class styles above the more specific ones. For example,
    first define a style for .Event, and below that for .SUCCESS

    The following CSS class names are used:
        - Activity, Event, BuildStep, LastBuild: general classes
        - waiting, interlocked, building, offline, idle: Activity states
        - start, running, success, failure, warnings, skipped, exception:
          LastBuild and BuildStep states
        - Change: box with change
        - Builder: box for builder name (at top)
        - Project
        - Time

    """

    # we are not a ComparableMixin, and therefore the webserver will be
    # rebuilt every time we reconfig.

    def __init__(self, http_port=None, distrib_port=None, allowForce=False):
        """Run a web server that provides Buildbot status.

        @type  http_port: int or L{twisted.application.strports} string
        @param http_port: a strports specification describing which port the
                          buildbot should use for its web server, with the
                          Waterfall display as the root page. For backwards
                          compatibility this can also be an int. Use
                          'tcp:8000' to listen on that port, or
                          'tcp:12345:interface=127.0.0.1' if you only want
                          local processes to connect to it (perhaps because
                          you are using an HTTP reverse proxy to make the
                          buildbot available to the outside world, and do not
                          want to make the raw port visible).

        @type  distrib_port: int or L{twisted.application.strports} string
        @param distrib_port: Use this if you want to publish the Waterfall
                             page using web.distrib instead. The most common
                             case is to provide a string that is an absolute
                             pathname to the unix socket on which the
                             publisher should listen
                             (C{os.path.expanduser(~/.twistd-web-pb)} will
                             match the default settings of a standard
                             twisted.web 'personal web server'). Another
                             possibility is to pass an integer, which means
                             the publisher should listen on a TCP socket,
                             allowing the web server to be on a different
                             machine entirely. Both forms are provided for
                             backwards compatibility; the preferred form is a
                             strports specification like
                             'unix:/home/buildbot/.twistd-web-pb'. Providing
                             a non-absolute pathname will probably confuse
                             the strports parser.
        @param allowForce: boolean, if True then the webserver will allow
                           visitors to trigger and cancel builds
        """

        service.MultiService.__init__(self)
        if type(http_port) is int:
            http_port = "tcp:%d" % http_port
        self.http_port = http_port
        if distrib_port is not None:
            if type(distrib_port) is int:
                distrib_port = "tcp:%d" % distrib_port
            if distrib_port[0] in "/~.": # pathnames
                distrib_port = "unix:%s" % distrib_port
        self.distrib_port = distrib_port
        self.allowForce = allowForce

        # this will be replaced once we've been attached to a parent (and
        # thus have a basedir and can reference BASEDIR/public_html/)
        root = static.Data("placeholder", "text/plain")
        self.site = server.Site(root)
        self.childrenToBeAdded = {}

        self.setupUsualPages()

        # the following items are accessed by HtmlResource when it renders
        # each page.
        self.site.buildbot_service = self
        self.header = HEADER
        self.head_elements = HEAD_ELEMENTS[:]
        self.body_attrs = BODY_ATTRS.copy()
        self.footer = FOOTER
        self.template_values = {}

        if self.http_port is not None:
            s = strports.service(self.http_port, self.site)
            s.setServiceParent(self)
        if self.distrib_port is not None:
            f = pb.PBServerFactory(distrib.ResourcePublisher(self.site))
            s = strports.service(self.distrib_port, f)
            s.setServiceParent(self)

    def setupUsualPages(self):
        #self.putChild("", IndexOrWaterfallRedirection())
        self.putChild("waterfall", WaterfallStatusResource())
        self.putChild("builders", BuildersResource())
        self.putChild("changes", ChangesResource())
        #self.putChild("schedulers", SchedulersResource())

        self.putChild("one_line_per_build", OneLinePerBuild())

    def __repr__(self):
        if self.http_port is None:
            return "<WebStatus on path %s>" % self.distrib_port
        if self.distrib_port is None:
            return "<WebStatus on port %s>" % self.http_port
        return "<WebStatus on port %s and path %s>" % (self.http_port,
                                                       self.distrib_port)

    def setServiceParent(self, parent):
        service.MultiService.setServiceParent(self, parent)
        self.setupSite()

    def setupSite(self):
        # this is responsible for creating the root resource. It isn't done
        # at __init__ time because we need to reference the parent's basedir.
        htmldir = os.path.join(self.parent.basedir, "public_html")
        if not os.path.isdir(htmldir):
            os.mkdir(htmldir)
        root = static.File(htmldir)
        log.msg("WebStatus using (%s)" % htmldir)

        for name, child_resource in self.childrenToBeAdded.iteritems():
            root.putChild(name, child_resource)

        self.site.resource = root

    def putChild(self, name, child_resource):
        """This behaves a lot like root.putChild() . """
        self.childrenToBeAdded[name] = child_resource

    def getStatus(self):
        return self.parent.getStatus()
    def getControl(self):
        if self.allowForce:
            return IControl(self.parent)
        return None

    def getPortnum(self):
        # this is for the benefit of unit tests
        s = list(self)[0]
        return s._port.getHost().port

# resources can get access to the IStatus by calling
# request.site.buildbot_service.getStatus()

# this is the compatibility class for the old waterfall. It is exactly like a
# regular WebStatus except that the root resource (e.g. http://buildbot.net/)
# always redirects to a WaterfallStatusResource, and the old arguments are
# mapped into the new resource-tree approach. In the normal WebStatus, the
# root resource either redirects the browser to /waterfall or serves
# BASEDIR/public_html/index.html, and favicon/robots.txt are provided by
# having the admin write actual files into BASEDIR/public_html/ .

# note: we don't use a util.Redirect here because HTTP requires that the
# Location: header provide an absolute URI, and it's non-trivial to figure
# out our absolute URI from here.

class Waterfall(WebStatus):

    if hasattr(sys, "frozen"):
        # all 'data' files are in the directory of our executable
        here = os.path.dirname(sys.executable)
        buildbot_icon = os.path.abspath(os.path.join(here, "buildbot.png"))
        buildbot_css = os.path.abspath(os.path.join(here, "classic.css"))
    else:
        # running from source
        # the icon is sibpath(__file__, "../buildbot.png") . This is for
        # portability.
        up = os.path.dirname
        buildbot_icon = os.path.abspath(os.path.join(up(up(up(__file__))),
                                                     "buildbot.png"))
        buildbot_css = os.path.abspath(os.path.join(up(__file__),
                                                    "classic.css"))

    compare_attrs = ["http_port", "distrib_port", "allowForce",
                     "categories", "css", "favicon", "robots_txt"]

    def __init__(self, http_port=None, distrib_port=None, allowForce=True,
                 categories=None, css=buildbot_css, favicon=buildbot_icon,
                 robots_txt=None):
        WebStatus.__init__(self, http_port, distrib_port, allowForce)
        self.css = css
        if css:
            data = open(css, "rb").read()
            self.putChild("buildbot.css", static.Data(data, "text/plain"))
        self.favicon = favicon
        self.robots_txt = robots_txt
        if favicon:
            data = open(favicon, "rb").read()
            self.putChild("favicon.ico", static.Data(data, "image/x-icon"))
        if robots_txt:
            data = open(robots_txt, "rb").read()
            self.putChild("robots.txt", static.Data(data, "text/plain"))
        self.putChild("", WaterfallStatusResource(categories))
