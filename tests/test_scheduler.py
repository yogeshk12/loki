"""
Specialised test that exercises the bulk-processing capabilities and
source-injection mechanism provided by the `loki.scheduler` and
`loki.task` sub-modules.

Test directory structure

 - projA:
   - include
     - another_l1.intfb.h
     - another_l2.intfb.h
   - source
     - another_l1
     - another_l2
   - module
     - header_mod
       * header_type
     - driverA_mod
     - kernelA_mod
     - compute_l1_mod
     - compute_l2_mod
     - driverB_mod
     - kernelB_mod

 - projB:
   - external
     - ext_driver_mod
   - module
     - ext_kernel_mod

 - projC:
   - util
     - proj_c_util_mod
       * routine_one
       * routine_two
"""

import pytest
from pathlib import Path
from collections import OrderedDict

from loki import (
    Scheduler, FP, SourceFile, FindNodes, CallStatement, fexprgen, Transformation
)


@pytest.fixture(scope='module', name='here')
def fixture_here():
    return Path(__file__).parent


def test_scheduler_taskgraph_simple(here):
    """
    Create a simple task graph from a single sub-project:

    projA: driverA -> kernelA -> compute_l1 -> compute_l2
                           |
                           | --> another_l1 -> another_l2
    """
    projA = here/'sources/projA'

    config = {
        'default': {
            'mode': 'idem',
            'role': 'kernel',
            'expand': True,
            'strict': True,
            'blacklist': []
        },
        'routines': []
    }

    scheduler = Scheduler(paths=projA, includes=projA/'include',
                          config=config, frontend=FP)
    scheduler.append('driverA')
    scheduler.populate()

    # TODO: Lower-casing of `kernela` is wrong, btu necessary for now. Fix!
    expected_nodes = ['driverA', 'kernela', 'compute_l1', 'compute_l2', 'another_l1', 'another_l2']
    expected_edges = [
        'driverA -> kernela',
        'kernela -> compute_l1',
        'compute_l1 -> compute_l2',
        'kernela -> another_l1',
        'another_l1 -> another_l2'
    ]

    nodes = [n.name for n in scheduler.taskgraph.nodes]
    edges = ['{} -> {}'.format(e1.name, e2.name) for e1, e2 in scheduler.taskgraph.edges]
    assert all(n in nodes for n in expected_nodes)
    assert all(e in edges for e in expected_edges)


def test_scheduler_taskgraph_partial(here):
    """
    Create a simple task graph from a single sub-project:

    projA: driverA -> kernelA -> compute_l1 -> compute_l2
                           |
                           | --> another_l1 -> another_l2
    """
    projA = here/'sources/projA'

    config = {
        'default': {
            'mode': 'idem',
            'role': 'kernel',
            'expand': True,
            'strict': True,
            'blacklist': []
        },
        'routine': [
            {
                'name': 'compute_l1',
                'role': 'driver',
                'expand': True,
            }, {
                'name': 'another_l1',
                'role': 'driver',
                'expand': True,
            },
        ]
    }
    # TODO: Note this is too convoluted, but mimicking the toml file config
    # reads we do in the current all-physics demonstrator. Needs internalising!
    config['routines'] = OrderedDict((r['name'], r) for r in config.get('routine', []))

    scheduler = Scheduler(paths=projA, includes=projA/'include', config=config)
    scheduler.append('driverA')
    scheduler.populate()

    # Check the correct sub-graph is generated
    nodes = [n.name for n in scheduler.taskgraph.nodes]
    edges = ['{} -> {}'.format(e1.name, e2.name) for e1, e2 in scheduler.taskgraph.edges]
    assert all(n in nodes for n in ['compute_l1', 'compute_l2', 'another_l1', 'another_l2'])
    assert all(e in edges for e in ['compute_l1 -> compute_l2', 'another_l1 -> another_l2'])


def test_scheduler_taskgraph_blocked(here):
    """
    Create a simple task graph with a single branch blocked:

    projA: driverA -> kernelA -> compute_l1 -> compute_l2
                           |
                           X --> <blocked>
    """
    projA = here/'sources/projA'

    config = {
        'default': {
            'mode': 'idem',
            'role': 'kernel',
            'expand': True,
            'strict': True,
            'blacklist': ['another_l1']
        },
        'routines': []
    }

    scheduler = Scheduler(paths=projA, includes=projA/'include',
                          config=config, frontend=FP)
    scheduler.append('driverA')
    scheduler.populate()

    expected_nodes = ['driverA', 'kernela', 'compute_l1', 'compute_l2']
    expected_edges = [
        'driverA -> kernela',
        'kernela -> compute_l1',
        'compute_l1 -> compute_l2',
    ]

    nodes = [n.name for n in scheduler.taskgraph.nodes]
    edges = ['{} -> {}'.format(e1.name, e2.name) for e1, e2 in scheduler.taskgraph.edges]
    assert all(n in nodes for n in expected_nodes)
    assert all(e in edges for e in expected_edges)

    assert 'another_l1' not in nodes
    assert 'another_l2' not in nodes
    assert 'kernela -> another_l1' not in edges
    assert 'another_l1 -> another_l2' not in edges


def test_scheduler_typedefs(here):
    """
    Create a simple task graph with and inject type info via `typedef`s.

    projA: driverA -> kernelA -> compute_l1 -> compute_l2
                           |
                     <header_type>
                           | --> another_l1 -> another_l2
    """
    projA = here/'sources/projA'

    config = {
        'default': {
            'mode': 'idem',
            'role': 'kernel',
            'expand': True,
            'strict': True,
            'blacklist': []
        },
        'routines': []
    }

    header = SourceFile.from_file(projA/'module/header_mod.f90', frontend=FP)

    scheduler = Scheduler(paths=projA, typedefs=header['header_mod'].typedefs,
                          includes=projA/'include', config=config, frontend=FP)
    scheduler.append('driverA')
    scheduler.populate()

    driver = scheduler.item_map['driverA'].routine
    call = FindNodes(CallStatement).visit(driver.body)[0]
    assert call.arguments[0].parent.type.variables
    assert fexprgen(call.arguments[0].shape) == '(:,)'
    assert call.arguments[1].parent.type.variables
    assert fexprgen(call.arguments[1].shape) == '(3, 3)'


def test_scheduler_process(here):
    """
    Create a simple task graph from a single sub-project
    and apply a simple transformation to it.

    projA: driverA -> kernelA -> compute_l1 -> compute_l2
                           |      <driver>      <kernel>
                           |
                           | --> another_l1 -> another_l2
                                  <driver>      <kernel>
    """
    projA = here/'sources/projA'

    config = {
        'default': {
            'mode': 'idem',
            'role': 'kernel',
            'expand': True,
            'strict': True,
            'blacklist': [],
            'whitelist': [],
        },
        'routine': [
            {
                'name': 'compute_l1',
                'role': 'driver',
                'expand': True,
            }, {
                'name': 'another_l1',
                'role': 'driver',
                'expand': True,
            },
        ]
    }
    config['routines'] = OrderedDict((r['name'], r) for r in config.get('routine', []))

    scheduler = Scheduler(paths=projA, includes=projA/'include', config=config)
    scheduler.append(config['routines'].keys())
    scheduler.populate()

    class AppendRole(Transformation):
        """
        Simply append role to subroutine names.
        """
        def transform_subroutine(self, routine, **kwargs):
            # TODO: This needs internalising!
            # Determine role in bulk-processing use case
            task = kwargs.get('task', None)
            role = kwargs.get('role') if task is None else task.config['role']
            routine.name += '_{}'.format(role)

    # Apply re-naming transformation and check result
    scheduler.process(transformation=AppendRole())
    assert scheduler.item_map['compute_l1'].routine.name == 'compute_l1_driver'
    assert scheduler.item_map['compute_l2'].routine.name == 'compute_l2_kernel'
    assert scheduler.item_map['another_l1'].routine.name == 'another_l1_driver'
    assert scheduler.item_map['another_l2'].routine.name == 'another_l2_kernel'


def test_scheduler_taskgraph_multiple_combined(here):
    """
    Create a single task graph spanning two projects

    projA: driverB -> kernelB -> compute_l1<replicated> -> compute_l2
                         |
    projB:          ext_driver -> ext_kernel
    """
    projA = here/'sources/projA'
    projB = here/'sources/projB'

    config = {
        'default': {
            'mode': 'idem',
            'role': 'kernel',
            'expand': True,
            'strict': True,
            'blacklist': []
        },
        'routines': [],
    }

    scheduler = Scheduler(paths=[projA, projB], includes=projA/'include', config=config)
    scheduler.append('driverB')
    scheduler.populate()

    expected_nodes = ['driverB', 'kernelb', 'compute_l1', 'compute_l2', 'ext_driver', 'ext_kernel']
    expected_edges = [
        'driverB -> kernelb',
        'kernelb -> compute_l1',
        'compute_l1 -> compute_l2',
        'kernelb -> ext_driver',
        'ext_driver -> ext_kernel'
    ]
    nodes = [n.name for n in scheduler.taskgraph.nodes]
    edges = ['{} -> {}'.format(e1.name, e2.name) for e1, e2 in scheduler.taskgraph.edges]
    assert all(n in nodes for n in expected_nodes)
    assert all(e in edges for e in expected_edges)


def test_scheduler_multiple_projects_ignore():
    """
    Create two distinct task graphs using `ignore` to prune the driver graph.

    projA: driverB -> kernelB -> compute_l1<replicated> -> compute_l2
                         |
                     <ext_driver>

    projB:            ext_driver -> ext_kernel
    """
    pass


def test_scheduler_multiple_projects_enrich():
    """
    Create two distinct task graphs using `ignore` to prune
    the driver graph and using `enrich` to enable IPA passes.

    projA: driverB -> kernelB -> compute_l1<replicated> -> compute_l2
                         |
                     <ext_driver>
                        < >
    projB:            ext_driver -> ext_kernel
    """
    pass


def test_scheduler_module_dependencies():
    """
    Ensure dependency chasing is done correctly, even with surboutines
    that do not match module names.

    projA: driverC -> kernelC -> compute_l1<replicated> -> compute_l2
                           |
    projC:                 | --> routine_one -> routine_two
    """
    pass


def test_scheduler_module_dependencies_unqualified():
    """
    Ensure dependency chasing is done correctly for unqualified module imports.
    """
    pass
