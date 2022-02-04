"""
Verify correct frontend behaviour and correct parsing of certain Fortran
language features.
"""
from pathlib import Path
import numpy as np
import pytest

from conftest import jit_compile, clean_test, available_frontends
from loki import (
    Module, Subroutine, FindNodes, FindVariables, Allocation, Deallocation, Associate,
    BasicType, OMNI, Enumeration
)
from loki.expression import symbols as sym


@pytest.fixture(scope='module', name='here')
def fixture_here():
    return Path(__file__).parent


@pytest.mark.parametrize('frontend', available_frontends())
def test_check_alloc_opts(here, frontend):
    """
    Test the use of SOURCE and STAT in allocate
    """

    fcode = """
module alloc_mod
  integer, parameter :: jprb = selected_real_kind(13,300)

  type explicit
    real(kind=jprb) :: scalar, vector(3), matrix(3, 3)
    real(kind=jprb) :: red_herring
  end type explicit

  type deferred
    real(kind=jprb), allocatable :: scalar, vector(:), matrix(:, :)
    real(kind=jprb), allocatable :: red_herring
  end type deferred
contains

  subroutine alloc_deferred(item)
    type(deferred), intent(inout) :: item
    integer :: stat
    allocate(item%vector(3), stat=stat)
    allocate(item%matrix(3, 3))
  end subroutine alloc_deferred

  subroutine free_deferred(item)
    type(deferred), intent(inout) :: item
    integer :: stat
    deallocate(item%vector, stat=stat)
    deallocate(item%matrix)
  end subroutine free_deferred

  subroutine check_alloc_source(item, item2)
    type(explicit), intent(inout) :: item
    type(deferred), intent(inout) :: item2
    real(kind=jprb), allocatable :: vector(:), vector2(:)

    allocate(vector, source=item%vector)
    vector(:) = vector(:) + item%scalar
    item%vector(:) = vector(:)

    allocate(vector2, source=item2%vector)  ! Try mold here when supported by fparser
    vector2(:) = item2%scalar
    item2%vector(:) = vector2(:)
  end subroutine check_alloc_source
end module alloc_mod
"""

    # Parse the source and validate the IR
    module = Module.from_source(fcode, frontend=frontend)

    allocations = FindNodes(Allocation).visit(module['check_alloc_source'].body)
    assert len(allocations) == 2
    assert all(alloc.data_source is not None for alloc in allocations)
    assert all(alloc.status_var is None for alloc in allocations)

    allocations = FindNodes(Allocation).visit(module['alloc_deferred'].body)
    assert len(allocations) == 2
    assert all(alloc.data_source is None for alloc in allocations)
    assert allocations[0].status_var is not None
    assert allocations[1].status_var is None

    deallocs = FindNodes(Deallocation).visit(module['free_deferred'].body)
    assert len(deallocs) == 2
    assert deallocs[0].status_var is not None
    assert deallocs[1].status_var is None

    # Sanity check for the backend
    assert module.to_fortran().lower().count(', stat=stat') == 2

    # Generate Fortran and test it
    filepath = here/(f'frontends_check_alloc_{frontend}.f90')
    mod = jit_compile(module, filepath=filepath, objname='alloc_mod')

    item = mod.explicit()
    item.scalar = 1.
    item.vector[:] = 1.

    item2 = mod.deferred()
    mod.alloc_deferred(item2)
    item2.scalar = 2.
    item2.vector[:] = -1.

    mod.check_alloc_source(item, item2)
    assert (item.vector == 2.).all()
    assert (item2.vector == 2.).all()
    mod.free_deferred(item2)

    clean_test(filepath)


@pytest.mark.parametrize('frontend', available_frontends())
def test_associates(here, frontend):
    """
    Test the use of associate to access and modify other items
    """

    fcode = """
module derived_types_mod
  integer, parameter :: jprb = selected_real_kind(13,300)

  type explicit
    real(kind=jprb) :: scalar, vector(3), matrix(3, 3)
    real(kind=jprb) :: red_herring
  end type explicit

  type deferred
    real(kind=jprb), allocatable :: scalar, vector(:), matrix(:, :)
    real(kind=jprb), allocatable :: red_herring
  end type deferred
contains

  subroutine alloc_deferred(item)
    type(deferred), intent(inout) :: item
    allocate(item%vector(3))
    allocate(item%matrix(3, 3))
  end subroutine alloc_deferred

  subroutine free_deferred(item)
    type(deferred), intent(inout) :: item
    deallocate(item%vector)
    deallocate(item%matrix)
  end subroutine free_deferred

  subroutine associates(item)
    type(explicit), intent(inout) :: item
    type(deferred) :: item2

    item%scalar = 17.0

    associate(vector2=>item%matrix(:,1))
        vector2(:) = 3.
        item%matrix(:,3) = vector2(:)
    end associate

    associate(vector=>item%vector)
        item%vector(2) = vector(1)
        vector(3) = item%vector(1) + vector(2)
        vector(1) = 1.
    end associate

    call alloc_deferred(item2)

    associate(vec=>item2%vector(2))
        vec = 1.
    end associate

    call free_deferred(item2)
  end subroutine associates
end module
"""
    # Test the internals
    module = Module.from_source(fcode, frontend=frontend)
    routine = module['associates']
    variables = FindVariables().visit(routine.body)
    if frontend == OMNI:
        assert all(v.shape == ('1:3',)
                   for v in variables if v.name in ['vector', 'vector2'])
    else:
        assert all(v.shape == ('3',)
                   for v in variables if v.name in ['vector', 'vector2'])

    for assoc in FindNodes(Associate).visit(routine.body):
        for var in FindVariables().visit(assoc.body):
            if var.name in assoc.variables:
                assert var.scope is assoc
                assert var.type.parent is None
            else:
                assert var.scope is routine

    # Test the generated module
    filepath = here/(f'derived_types_associates_{frontend}.f90')
    mod = jit_compile(module, filepath=filepath, objname='derived_types_mod')

    item = mod.explicit()
    item.scalar = 0.
    item.vector[0] = 5.
    item.vector[1:2] = 0.
    mod.associates(item)
    assert item.scalar == 17.0 and (item.vector == [1., 5., 10.]).all()

    clean_test(filepath)


@pytest.mark.parametrize('frontend', available_frontends(xfail=[(OMNI, 'OMNI fails to read without full module')]))
def test_associates_deferred(frontend):
    """
    Verify that reading in subroutines with deferred external type definitions
    and associates working on that are supported.
    """

    fcode = """
SUBROUTINE ASSOCIATES_DEFERRED(ITEM, IDX)
USE SOME_MOD, ONLY: SOME_TYPE
IMPLICIT NONE
TYPE(SOME_TYPE), INTENT(IN) :: ITEM
INTEGER, INTENT(IN) :: IDX
ASSOCIATE(SOME_VAR=>ITEM%SOME_VAR(IDX))
SOME_VAR = 5
END ASSOCIATE
END SUBROUTINE
    """
    routine = Subroutine.from_source(fcode, frontend=frontend)
    assert len(FindVariables(recurse_to_parent=False).visit(routine.body)) == 3
    variables = {v.name: v for v in FindVariables().visit(routine.body)}
    assert len(variables) == 4
    some_var = variables['SOME_VAR']
    assert isinstance(some_var, sym.DeferredTypeSymbol)
    assert some_var.name.upper() == 'SOME_VAR'
    assert some_var.type.dtype == BasicType.DEFERRED
    assert some_var.scope is FindNodes(Associate).visit(routine.body)[0]


@pytest.mark.parametrize('frontend', available_frontends())
def test_associates_expr(here, frontend):
    """
    Verify that associates with expressions are supported
    """
    fcode = """
subroutine associates_expr(in, out)
  implicit none
  integer, intent(in) :: in(3)
  integer, intent(out) :: out(3)

  out(:) = 0

  associate(a=>1+3)
    out(:) = out(:) + a
  end associate

  associate(b=>2*in(:) + in(:))
    out(:) = out(:) + b(:)
  end associate
end subroutine associates_expr
    """.strip()
    routine = Subroutine.from_source(fcode, frontend=frontend)

    variables = {v.name: v for v in FindVariables().visit(routine.body)}
    assert len(variables) == 4
    assert isinstance(variables['a'], sym.DeferredTypeSymbol)
    assert variables['a'].type.dtype is BasicType.DEFERRED  # TODO: support type derivation for expressions
    assert isinstance(variables['b'], sym.Array)  # Note: this is an array because we have a shape
    assert variables['b'].type.dtype is BasicType.DEFERRED  # TODO: support type derivation for expressions
    assert variables['b'].type.shape == ('3',)

    filepath = here/(f'associates_expr_{frontend}.f90')
    function = jit_compile(routine, filepath=filepath, objname=routine.name)
    a = np.array([1, 2, 3], dtype='i')
    b = np.zeros(3, dtype='i')
    function(a, b)
    assert np.all(b == [7, 10, 13])
    clean_test(filepath)


@pytest.mark.parametrize('frontend', available_frontends())
def test_enum(here, frontend):
    """
    Verify that enums are represented correctly
    """
    # F2008, Note 4.67
    fcode = """
subroutine test_enum (out)
    implicit none

    ! Comment 1
    ENUM, BIND(C)
        ENUMERATOR :: RED = 4, BLUE = 9
        ! Comment 2
        ENUMERATOR YELLOW
    END ENUM
    ! Comment 3

    integer, intent(out) :: out

    out = RED + BLUE + YELLOW
end subroutine test_enum
    """.strip()

    routine = Subroutine.from_source(fcode, frontend=frontend)

    # Check Enum exists
    enums = FindNodes(Enumeration).visit(routine.spec)
    assert len(enums) == 1

    # Check symbols are available
    assert enums[0].symbols == ('red', 'blue', 'yellow')
    assert all(name in routine.symbols for name in ('red', 'blue', 'yellow'))
    assert all(s.scope is routine for s in enums[0].symbols)

    # Check assigned values
    assert routine.symbol_map['red'].type.initial == '4'
    assert routine.symbol_map['blue'].type.initial == '9'
    assert routine.symbol_map['yellow'].type.initial is None

    # Verify comments are preserved (don't care about the actual place)
    code = routine.to_fortran()
    for i in range(1, 4):
        assert f'! Comment {i}' in code

    # Check fgen produces valid code and runs
    filepath = here/(f'{routine.name}_{frontend}.f90')
    function = jit_compile(routine, filepath=filepath, objname=routine.name)
    out = function()
    assert out == 23
    clean_test(filepath)