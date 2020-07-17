from pathlib import Path
from collections import deque, OrderedDict
import networkx as nx
try:
    import graphviz as gviz
except ImportError:
    gviz = None

from loki.build import Obj
from loki.frontend import FP
from loki.ir import CallStatement
from loki.visitors import FindNodes
from loki.sourcefile import SourceFile
from loki.tools import as_tuple, find_files
from loki.logging import info, warning, error


__all__ = ['Task', 'Scheduler']


class Task:
    """
    A work item that represents a single source routine or module to
    be processed. Each :class:`Task` spawns new work items according
    to its own subroutine calls and the scheduler's blacklist.

    Note: Each work item may have its own configuration settings that
    primarily inherit values from the 'default', but can be
    specialised explicitly in the config file.
    """

    def __init__(self, name, config, path, graph=None, xmods=None,
                 includes=None, builddir=None, typedefs=None, frontend=FP):
        self.name = name
        self.path = path
        self.file = None
        self.routine = None
        self.graph = graph
        self.frontend = frontend

        # Generate item-specific config settings
        self.config = config['default'].copy()
        if name in config['routines']:
            self.config.update(config['routines'][name])

        if path.exists():
            try:
                # Read and parse source file and extract subroutine
                self.file = SourceFile.from_file(path, preprocess=True,
                                                 xmods=xmods, includes=includes,
                                                 builddir=builddir,
                                                 typedefs=typedefs, frontend=frontend)
                self.routine = self.file.all_subroutines[0]

            except Exception as excinfo:  # pylint: disable=broad-except
                if self.graph:
                    self.graph.node(self.name.upper(), color='red', style='filled')

                warning('Could not parse %s:', path)
                if self.config['strict']:
                    raise excinfo
                error(excinfo)

        else:
            if self.graph:
                self.graph.node(self.name.upper(), color='lightsalmon', style='filled')
            info("Could not find source file %s; skipping...", name)

    @property
    def children(self):
        """
        Set of all child routines that this work item calls.
        """
        members = [m.name.lower() for m in (self.routine.members or [])]
        return tuple(call.name for call in FindNodes(CallStatement).visit(self.routine.ir)
                     if call.name.lower() not in members)

    def enrich(self, routines):
        self.routine.enrich_calls(routines=routines)


class Scheduler:
    """
    Work queue manager to enqueue and process individual :class:`Task`
    routines/modules with a given kernel.

    Note: The processing module can create a callgraph and perform
    automated discovery, to enable easy bulk-processing of large
    numbers of source files.

    :param paths: List of locations to search for source files.
    """

    _deadlist = ['dr_hook', 'abor1', 'abort_surf', 'flush']

    # TODO: Should be user-definable!
    source_suffixes = ['.f90', '_mod.f90']

    def __init__(self, paths, config=None, xmods=None, includes=None,
                 builddir=None, typedefs=None, frontend=FP):
        self.paths = [Path(p) for p in as_tuple(paths)]
        self.config = config
        self.xmods = xmods
        self.includes = includes
        self.typedefs = typedefs
        self.frontend = frontend
        # TODO: Remove; should be done per item
        self.blacklist = []

        self.builddir = builddir

        self.taskgraph = nx.DiGraph()

        self.queue = deque()
        self.processed = []
        self.item_map = {}

        if gviz is not None:
            self.graph = gviz.Digraph(format='pdf', strict=True)
        else:
            self.graph = None

        # Scan all source paths and create light-weight `Obj` objects for each file.
        obj_list = []
        for path in self.paths:
            for ext in Obj._ext:
                obj_list += [Obj(source_path=f) for f in path.glob('**/*%s' % ext)]

        # Create a map of all potential target routines for fast lookup later
        self.obj_map = OrderedDict((r, obj) for obj in obj_list for r in obj.subroutines)

    @property
    def routines(self):
        return [task.routine for task in self.taskgraph.nodes]

    def find_path(self, routine):
        """
        Find path of file containing a given routine from the internal `obj_cache`.

        :param routine: Name of the source routine to locate.
        """
        if routine in self.obj_map:
            return self.obj_map[routine].source_path

        raise RuntimeError("Source path not found for routine: %s" % routine)

    def append(self, sources):
        """
        Add names of source routines or modules to find and process.
        """
        for source in as_tuple(sources):
            if source in self.item_map:
                continue
            item = Task(name=source, config=self.config,
                        path=self.find_path(source),
                        graph=self.graph, xmods=self.xmods,
                        includes=self.includes, typedefs=self.typedefs,
                        builddir=self.builddir,
                        frontend=self.frontend)
            self.queue.append(item)
            self.item_map[source] = item

            self.taskgraph.add_node(item)

    def populate(self):
        """
        Process all enqueued source modules and routines with the
        stored kernel.
        """

        while len(self.queue) > 0:
            item = self.queue.popleft()

            for child in item.children:
                # Skip "deadlisted" items immediately
                if child in self._deadlist:
                    continue

                # Mark blacklisted children in graph, but skip
                if child in item.config['blacklist']:
                    if self.graph:
                        self.graph.node(child.upper(), color='black',
                                        fillcolor='orangered', style='filled')
                        self.graph.edge(item.name.upper(), child.upper())

                    continue

                # Append child to work queue if expansion is configured
                if item.config['expand']:
                    # Do not propagate to dependencies marked as "ignore"
                    # Note that, unlike blackisted items, "ignore" items
                    # are still marked as targets during bulk-processing,
                    # so that calls to "ignore" routines will be renamed.
                    ignore_list = item.config.get('ignore', [])
                    if child in ignore_list:
                        if self.graph:
                            self.graph.node(child.upper(), color='black', shape='box'
                                            , fillcolor='lightblue', style='filled')
                            self.graph.edge(item.name.upper(), child.upper())
                        continue

                    self.append(child)

                    self.taskgraph.add_edge(item, self.item_map[child])

                    # Append newly created edge to graph
                    if self.graph:
                        if child not in [r.name for r in self.processed]:
                            self.graph.node(child.upper(), color='black',
                                            fillcolor='lightblue', style='filled')
                        self.graph.edge(item.name.upper(), child.upper())

        # Enrich subroutine calls for inter-procedural transformations
        for item in self.taskgraph:
            item.enrich(routines=self.routines)

            if 'enrich' in item.config:
                for routine in item.config['enrich']:
                    path = self.find_path(routine)
                    source = SourceFile.from_file(path, preprocess=True,
                                                  xmods=self.xmods,
                                                  includes=self.includes,
                                                  builddir=self.builddir,
                                                  typedefs=self.typedefs,
                                                  frontend=self.frontend)
                    item.routine.enrich_calls(source.all_subroutines)

    def process(self, transformation):
        """
        Process all enqueued source modules and routines with the
        stored kernel. The traversal is performed in topological
        order, which ensures that :class:`CallStatement`s are always processed
        before their target :class:`Subroutine`s.
        """
        for task in nx.topological_sort(self.taskgraph):

            # Process work item with appropriate kernel
            transformation.apply(task.routine, task=task, processor=self)

            # Mark item as processed in list and graph
            self.processed.append(task)

            if self.graph:
                if task.name in task.config['whitelist']:
                    self.graph.node(task.name.upper(), color='black', shape='diamond',
                                    fillcolor='limegreen', style='rounded,filled')
                else:
                    self.graph.node(task.name.upper(), color='black',
                                    fillcolor='limegreen', style='filled')
