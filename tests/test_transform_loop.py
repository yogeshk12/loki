from pathlib import Path
import pytest
import numpy as np

from conftest import jit_compile, clean_test
from loki import Subroutine, OFP, OMNI, FP, FindNodes, Loop, Conditional, Scope
from loki.frontend.fparser import parse_fparser_expression
from loki.transform import loop_fusion, loop_fission, Polyhedron
from loki.expression import symbols as sym


@pytest.fixture(scope='module', name='here')
def fixture_here():
    return Path(__file__).parent


@pytest.mark.parametrize('variables, lbounds, ubounds, A, b, variable_names', [
    # do i=0,5: do j=i,7: ...
    (['i', 'j'], ['0', 'i'], ['5', '7'],
     [[-1, 0], [1, 0], [1, -1], [0, 1]], [0, 5, 0, 7], ['i', 'j']),
    # do i=1,n: do j=0,2*i+1: do k=a,b: ...
    (['i', 'j', 'k'], ['1', '0', 'a'], ['n', '2*i+1', 'b'],
     [[-1, 0, 0, 0, 0, 0], [1, 0, 0, 0, 0, -1], [0, -1, 0, 0, 0, 0], [-2, 1, 0, 0, 0, 0],
      [0, 0, -1, 1, 0, 0], [0, 0, 1, 0, -1, 0]], [-1, 0, 0, 1, 0, 0], ['i', 'j', 'k', 'a', 'b', 'n']),
    # do jk=1,klev: ...
    (['jk'], ['1'], ['klev'], [[-1, 0], [1, -1]], [-1, 0], ['jk', 'klev']),
    # do JK=1,klev-1: ...
    (['JK'], ['1'], ['klev - 1'], [[-1, 0], [1, -1]], [-1, -1], ['jk', 'klev']),
    # do jk=ncldtop,klev: ...
    (['jk'], ['ncldtop'], ['klev'], [[-1, 0, 1], [1, -1, 0]], [0, 0], ['jk', 'klev', 'ncldtop']),
    # do jk=1,KLEV+1: ...
    (['jk'], ['1'], ['KLEV+1'], [[-1, 0], [1, -1]], [-1, 1], ['jk', 'klev']),
])
def test_polyhedron_from_loop_ranges(variables, lbounds, ubounds, A, b, variable_names):
    """
    Test converting loop ranges to polyedron representation of iteration space.
    """
    scope = Scope()
    loop_variables = [parse_fparser_expression(expr, scope) for expr in variables]
    loop_lbounds = [parse_fparser_expression(expr, scope) for expr in lbounds]
    loop_ubounds = [parse_fparser_expression(expr, scope) for expr in ubounds]
    loop_ranges = [sym.LoopRange((l, u)) for l, u in zip(loop_lbounds, loop_ubounds)]
    p = Polyhedron.from_loop_ranges(loop_variables, loop_ranges)
    assert np.all(p.A == np.array(A, dtype=np.dtype(int)))
    assert np.all(p.b == np.array(b, dtype=np.dtype(int)))
    assert p.variables == variable_names


def test_polyhedron_from_loop_ranges_failures():
    """
    Test known limitation of the conversion from loop ranges to polyhedron.
    """
    # m*n is non-affine and thus can't be represented
    scope = Scope()
    loop_variable = parse_fparser_expression('i', scope)
    lower_bound = parse_fparser_expression('1', scope)
    upper_bound = parse_fparser_expression('m * n', scope)
    loop_range = sym.LoopRange((lower_bound, upper_bound))
    with pytest.raises(ValueError):
        _ = Polyhedron.from_loop_ranges([loop_variable], [loop_range])

    # no functionality to flatten exponentials, yet
    upper_bound = parse_fparser_expression('5**2', scope)
    loop_range = sym.LoopRange((lower_bound, upper_bound))
    with pytest.raises(ValueError):
        _ = Polyhedron.from_loop_ranges([loop_variable], [loop_range])


@pytest.mark.parametrize('A, b, variable_names, lower_bounds, upper_bounds', [
    # do i=1,n: ...
    ([[-1, 0], [1, -1]], [-1, 0], ['i', 'n'], [['1'], ['i']], [['n'], []]),
    # do i=1,10: ...
    ([[-1], [1]], [-1, 10], ['i'], [['1']], [['10']]),
    # do i=0,5: do j=i,7: ...
    ([[-1, 0], [1, 0], [1, -1], [0, 1]], [0, 5, 0, 7], ['i', 'j'], [['0'], ['i']], [['5', 'j'], ['7']]),
    # do i=1,n: do j=0,2*i+1: do k=a,b: ...
    ([[-1, 0, 0, 0, 0, 0], [1, 0, 0, 0, 0, -1], [0, -1, 0, 0, 0, 0], [-2, 1, 0, 0, 0, 0],
      [0, 0, -1, 1, 0, 0], [0, 0, 1, 0, -1, 0]], [-1, 0, 0, 1, 0, 0],
     ['i', 'j', 'k', 'a', 'b', 'n'],                               # variable names
     [['1', '-1 / 2 + j / 2'], ['0'], ['a'], [], ['k'], ['i']],    # lower bounds
     [['n'], ['1 + 2*i'], ['b'], ['k'], [], []]),                  # upper bounds
])
def test_polyhedron_bounds(A, b, variable_names, lower_bounds, upper_bounds):
    """
    Test the production of lower and upper bounds.
    """
    scope = Scope()
    variables = [parse_fparser_expression(v, scope) for v in variable_names]
    p = Polyhedron(A, b, variables)
    for var, ref_bounds in zip(variables, lower_bounds):
        lbounds = p.lower_bounds(var)
        assert len(lbounds) == len(ref_bounds)
        assert all(str(b1) == b2 for b1, b2 in zip(lbounds, ref_bounds))
    for var, ref_bounds in zip(variables, upper_bounds):
        ubounds = p.upper_bounds(var)
        assert len(ubounds) == len(ref_bounds)
        assert all(str(b1) == b2 for b1, b2 in zip(ubounds, ref_bounds))


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fuse_matching(here, frontend):
    """
    Apply loop fusion for two loops with matching iteration spaces.
    """
    fcode = """
subroutine transform_loop_fuse_matching(a, b, n)
  integer, intent(out) :: a(n), b(n)
  integer, intent(in) :: n
  integer :: i

  !$loki loop-fusion
  do i=1,n
    a(i) = i
  end do

  !$loki loop-fusion
  do i=1,n
    b(i) = n-i+1
  end do
end subroutine transform_loop_fuse_matching
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    n = 100
    a = np.zeros(shape=(n,), dtype=np.int32)
    b = np.zeros(shape=(n,), dtype=np.int32)
    function(a=a, b=b, n=n)
    assert np.all(a == range(1, n+1))
    assert np.all(b == range(n, 0, -1))

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 2
    loop_fusion(routine)
    assert len(FindNodes(Loop).visit(routine.body)) == 1

    fused_filepath = here/('%s_fused_%s.f90' % (routine.name, frontend))
    fused_function = jit_compile(routine, filepath=fused_filepath, objname=routine.name)

    # Test transformation
    a = np.zeros(shape=(n,), dtype=np.int32)
    b = np.zeros(shape=(n,), dtype=np.int32)
    fused_function(a=a, b=b, n=n)
    assert np.all(a == range(1, n+1))
    assert np.all(b == range(n, 0, -1))

    clean_test(filepath)
    clean_test(fused_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fuse_subranges(here, frontend):
    """
    Apply loop fusion with annotated range for loops with
    non-matching iteration spaces.
    """
    fcode = """
subroutine transform_loop_fuse_subranges(a, b, n)
  integer, intent(out) :: a(n), b(n)
  integer, intent(in) :: n
  integer :: i, j

  a(:) = 0
  b(:) = 0

  !$loki loop-fusion
  do i=1,n
    a(i) = a(i) + i
  end do

  !$loki loop-fusion range(1:n)
  do j=1,15
    b(j) = b(j) + n-j+1
  end do

  !$loki loop-fusion range(1:n)
  do i=16,n
    b(i) = b(i) + n-i+1
  end do
end subroutine transform_loop_fuse_subranges
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    n = 100
    a = np.zeros(shape=(n,), dtype=np.int32)
    b = np.zeros(shape=(n,), dtype=np.int32)
    function(a=a, b=b, n=n)
    assert np.all(a == range(1, n+1))
    assert np.all(b == range(n, 0, -1))

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 3
    loop_fusion(routine)
    assert len(FindNodes(Loop).visit(routine.body)) == 1

    fused_filepath = here/('%s_fused_%s.f90' % (routine.name, frontend))
    fused_function = jit_compile(routine, filepath=fused_filepath, objname=routine.name)

    # Test transformation
    a = np.zeros(shape=(n,), dtype=np.int32)
    b = np.zeros(shape=(n,), dtype=np.int32)
    fused_function(a=a, b=b, n=n)
    assert np.all(a == range(1, n+1))
    assert np.all(b == range(n, 0, -1))

    clean_test(filepath)
    clean_test(fused_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fuse_groups(here, frontend):
    """
    Apply loop fusion for multiple loop fusion groups.
    """
    fcode = """
subroutine transform_loop_fuse_groups(a, b, c, n)
  integer, intent(out) :: a(n), b(n), c(n)
  integer, intent(in) :: n
  integer :: i

  c(1) = 1

  !$loki loop-fusion group(g1)
  do i=1,n
    a(i) = i
  end do

  !$loki loop-fusion group(g1)
  do i=1,n
    b(i) = n-i+1
  end do

  !$loki loop-fusion group(loop-group2)
  do i=1,n
    a(i) = a(i) + 1
  end do

  !$loki loop-fusion group(loop-group2)
  do i=1,n
    b(i) = b(i) + 1
  end do

  !$loki loop-fusion group(g1) range(1:n)
  do i=2,n
    c(i) = c(i-1) + 1
  end do
end subroutine transform_loop_fuse_groups
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    n = 100
    a = np.zeros(shape=(n,), dtype=np.int32)
    b = np.zeros(shape=(n,), dtype=np.int32)
    c = np.zeros(shape=(n,), dtype=np.int32)
    function(a=a, b=b, c=c, n=n)
    assert np.all(a == range(2, n+2))
    assert np.all(b == range(n+1, 1, -1))
    assert np.all(c == range(1, n+1))

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 5
    loop_fusion(routine)
    assert len(FindNodes(Loop).visit(routine.body)) == 2

    fused_filepath = here/('%s_fused_%s.f90' % (routine.name, frontend))
    fused_function = jit_compile(routine, filepath=fused_filepath, objname=routine.name)

    # Test transformation
    a = np.zeros(shape=(n,), dtype=np.int32)
    b = np.zeros(shape=(n,), dtype=np.int32)
    c = np.zeros(shape=(n,), dtype=np.int32)
    fused_function(a=a, b=b, c=c, n=n)
    assert np.all(a == range(2, n+2))
    assert np.all(b == range(n+1, 1, -1))
    assert np.all(c == range(1, n+1))

    clean_test(filepath)
    clean_test(fused_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fuse_failures(frontend):
    """
    Test that loop-fusion fails for known mistakes.
    """
    fcode = """
subroutine transform_loop_fuse_failures(a, b, n)
  integer, intent(out) :: a(n), b(n)
  integer, intent(in) :: n
  integer :: i

  !$loki loop-fusion group(1) range(1:n)
  do i=1,n
    a(i) = i
  end do

  !$loki loop-fusion group(1) range(0:n-1)
  do i=0,n-1
    b(i+1) = n-i
  end do
end subroutine transform_loop_fuse_failures
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    with pytest.raises(RuntimeError):
        loop_fusion(routine)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fuse_alignment(here, frontend):
    fcode = """
subroutine transform_loop_fuse_alignment(a, b, n)
  integer, intent(out) :: a(n), b(n)
  integer, intent(in) :: n
  integer :: i

  !$loki loop-fusion group(1)
  do i=1,n
    a(i) = i
  end do

  !$loki loop-fusion group(1)
  do i=0,n-1
    b(i+1) = n-i
  end do
end subroutine transform_loop_fuse_alignment
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    n = 100
    a = np.zeros(shape=(n,), dtype=np.int32)
    b = np.zeros(shape=(n,), dtype=np.int32)
    function(a=a, b=b, n=n)
    assert np.all(a == range(1, n+1))
    assert np.all(b == range(n, 0, -1))

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 2
    loop_fusion(routine)
    assert len(FindNodes(Loop).visit(routine.body)) == 1

    fused_filepath = here/('%s_fused_%s.f90' % (routine.name, frontend))
    fused_function = jit_compile(routine, filepath=fused_filepath, objname=routine.name)

    # Test transformation
    a = np.zeros(shape=(n,), dtype=np.int32)
    b = np.zeros(shape=(n,), dtype=np.int32)
    fused_function(a=a, b=b, n=n)
    assert np.all(a == range(1, n+1))
    assert np.all(b == range(n, 0, -1))

    clean_test(filepath)
    clean_test(fused_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fuse_nonmatching_lower(here, frontend):
    fcode = """
subroutine transform_loop_fuse_nonmatching_lower(a, b, nclv, klev)
  integer, intent(out) :: a(klev), b(klev)
  integer, intent(in) :: nclv, klev
  integer :: jl

  !$loki loop-fusion group(1)
  do jl=1,klev
    a(jl) = jl
  end do

  !$loki loop-fusion group(1)
  do jl=nclv,klev
    b(jl) = jl - nclv
  end do
end subroutine transform_loop_fuse_nonmatching_lower
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    klev, nclv = 100, 15
    a = np.zeros(shape=(klev,), dtype=np.int32)
    b = np.zeros(shape=(klev,), dtype=np.int32)
    function(a=a, b=b, klev=klev, nclv=nclv)
    assert np.all(a == range(1, klev+1))
    assert np.all(b[nclv:klev+1] == range(1, klev-nclv+1))

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 2
    loop_fusion(routine)

    loops = FindNodes(Loop).visit(routine.body)
    assert len(loops) == 1
    assert isinstance(loops[0].bounds.start, sym.InlineCall) and loops[0].bounds.start.name == 'min'
    assert loops[0].bounds.stop == 'klev'
    assert len(FindNodes(Conditional).visit(routine.body)) == 2

    fused_filepath = here/('%s_fused_%s.f90' % (routine.name, frontend))
    fused_function = jit_compile(routine, filepath=fused_filepath, objname=routine.name)

    # Test transformation
    klev, nclv = 100, 15
    a = np.zeros(shape=(klev,), dtype=np.int32)
    b = np.zeros(shape=(klev,), dtype=np.int32)
    fused_function(a=a, b=b, klev=klev, nclv=nclv)
    assert np.all(a == range(1, klev+1))
    assert np.all(b[nclv:klev+1] == range(1, klev-nclv+1))

    clean_test(filepath)
    clean_test(fused_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fuse_nonmatching_lower_annotated(here, frontend):
    fcode = """
subroutine transform_loop_fuse_nonmatching_lower_annotated(a, b, nclv, klev)
  integer, intent(out) :: a(klev), b(klev)
  integer, intent(in) :: nclv, klev
  integer :: jl

  !$loki loop-fusion group(1)
  do jl=1,klev
    a(jl) = jl
  end do

  !$loki loop-fusion group(1) range(1:klev)
  do jl=nclv,klev
    b(jl) = jl - nclv
  end do
end subroutine transform_loop_fuse_nonmatching_lower_annotated
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    klev, nclv = 100, 15
    a = np.zeros(shape=(klev,), dtype=np.int32)
    b = np.zeros(shape=(klev,), dtype=np.int32)
    function(a=a, b=b, klev=klev, nclv=nclv)
    assert np.all(a == range(1, klev+1))
    assert np.all(b[nclv:klev+1] == range(1, klev-nclv+1))

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 2
    loop_fusion(routine)

    loops = FindNodes(Loop).visit(routine.body)
    assert len(loops) == 1
    assert loops[0].bounds.start == '1'
    assert loops[0].bounds.stop == 'klev'
    assert len(FindNodes(Conditional).visit(routine.body)) == 1

    fused_filepath = here/('%s_fused_%s.f90' % (routine.name, frontend))
    fused_function = jit_compile(routine, filepath=fused_filepath, objname=routine.name)

    # Test transformation
    klev, nclv = 100, 15
    a = np.zeros(shape=(klev,), dtype=np.int32)
    b = np.zeros(shape=(klev,), dtype=np.int32)
    fused_function(a=a, b=b, klev=klev, nclv=nclv)
    assert np.all(a == range(1, klev+1))
    assert np.all(b[nclv:klev+1] == range(1, klev-nclv+1))

    clean_test(filepath)
    clean_test(fused_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fuse_nonmatching_upper(here, frontend):
    fcode = """
subroutine transform_loop_fuse_nonmatching_upper(a, b, klev)
  integer, intent(out) :: a(klev), b(klev+1)
  integer, intent(in) :: klev
  integer :: jl

  !$loki loop-fusion group(1)
  do jl=1,klev
    a(jl) = jl
  end do

  !$loki loop-fusion group(1)
  do jl=1,klev+1
    b(jl) = 2*jl
  end do
end subroutine transform_loop_fuse_nonmatching_upper
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    klev = 100
    a = np.zeros(shape=(klev,), dtype=np.int32)
    b = np.zeros(shape=(klev+1,), dtype=np.int32)
    function(a=a, b=b, klev=klev)
    assert np.all(a == range(1, klev+1))
    assert np.all(b == np.array(list(range(1, klev+2))) * 2)

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 2
    loop_fusion(routine)

    loops = FindNodes(Loop).visit(routine.body)
    assert len(loops) == 1
    assert loops[0].bounds.start == '1'
    assert loops[0].bounds.stop == '1 + klev'
    assert len(FindNodes(Conditional).visit(routine.body)) == 1

    fused_filepath = here/('%s_fused_%s.f90' % (routine.name, frontend))
    fused_function = jit_compile(routine, filepath=fused_filepath, objname=routine.name)

    # Test transformation
    klev = 100
    a = np.zeros(shape=(klev,), dtype=np.int32)
    b = np.zeros(shape=(klev+1,), dtype=np.int32)
    fused_function(a=a, b=b, klev=klev)
    assert np.all(a == range(1, klev+1))
    assert np.all(b == np.array(list(range(1, klev+2))) * 2)

    clean_test(filepath)
    clean_test(fused_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fuse_collapse(here, frontend):
    fcode = """
subroutine transform_loop_fuse_collapse(a, b, klon, klev)
  integer, intent(inout) :: a(klon, klev), b(klon, klev)
  integer, intent(in) :: klon, klev
  integer :: jk, jl

!$loki loop-fusion collapse(2)
  do jk=1,klev
    do jl=1,klon
      a(jl, jk) = jk
    end do
  end do

!$loki loop-fusion collapse(2)
  do jk=1,klev
    do jl=1,klon
      b(jl, jk) = jl + jk
    end do
  end do
end subroutine transform_loop_fuse_collapse
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    klon, klev = 32, 100
    a = np.zeros(shape=(klon, klev), order='F', dtype=np.int32)
    b = np.zeros(shape=(klon, klev), order='F', dtype=np.int32)
    function(a=a, b=b, klon=klon, klev=klev)
    assert np.all(a == np.array([list(range(1, klev+1))] * klon, order='F'))
    assert np.all(b == np.array([[jl + jk for jk in range(1, klev+1)]
                                for jl in range(1, klon+1)], order='F'))

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 4
    loop_fusion(routine)
    loops = FindNodes(Loop).visit(routine.body)
    assert len(loops) == 2
    assert all(loop.bounds.start == '1' for loop in loops)
    assert sum([loop.bounds.stop == 'klev' for loop in loops]) == 1
    assert sum([loop.bounds.stop == 'klon' for loop in loops]) == 1

    fused_filepath = here/('%s_fused_%s.f90' % (routine.name, frontend))
    fused_function = jit_compile(routine, filepath=fused_filepath, objname=routine.name)

    # Test transformation
    klon, klev = 32, 100
    a = np.zeros(shape=(klon, klev), order='F', dtype=np.int32)
    b = np.zeros(shape=(klon, klev), order='F', dtype=np.int32)
    fused_function(a=a, b=b, klon=klon, klev=klev)
    assert np.all(a == np.array([list(range(1, klev+1))] * klon, order='F'))
    assert np.all(b == np.array([[jl + jk for jk in range(1, klev+1)]
                                for jl in range(1, klon+1)], order='F'))

    clean_test(filepath)
    clean_test(fused_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fuse_collapse_nonmatching(here, frontend):
    fcode = """
subroutine transform_loop_fuse_collapse_nonmatching(a, b, klon, klev)
  integer, intent(inout) :: a(klon, klev+1), b(klon+1, klev)
  integer, intent(in) :: klon, klev
  integer :: jk, jl

!$loki loop-fusion collapse(2)
  do jk=1,klev+1
    do jl=1,klon
      a(jl, jk) = jk
    end do
  end do

!$loki loop-fusion collapse(2)
  do jk=1,klev
    do jl=1,klon+1
      b(jl, jk) = jl + jk
    end do
  end do
end subroutine transform_loop_fuse_collapse_nonmatching
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    klon, klev = 32, 100
    a = np.zeros(shape=(klon, klev+1), order='F', dtype=np.int32)
    b = np.zeros(shape=(klon+1, klev), order='F', dtype=np.int32)
    function(a=a, b=b, klon=klon, klev=klev)
    assert np.all(a == np.array([list(range(1, klev+2))] * klon, order='F'))
    assert np.all(b == np.array([[jl + jk for jk in range(1, klev+1)]
                                for jl in range(1, klon+2)], order='F'))

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 4
    loop_fusion(routine)
    loops = FindNodes(Loop).visit(routine.body)
    assert len(loops) == 2
    assert all(loop.bounds.start == '1' for loop in loops)
    assert sum([loop.bounds.stop == '1 + klev' for loop in loops]) == 1
    assert sum([loop.bounds.stop == '1 + klon' for loop in loops]) == 1
    assert len(FindNodes(Conditional).visit(routine.body)) == 2

    fused_filepath = here/('%s_fused_%s.f90' % (routine.name, frontend))
    fused_function = jit_compile(routine, filepath=fused_filepath, objname=routine.name)

    # Test transformation
    klon, klev = 32, 100
    a = np.zeros(shape=(klon, klev+1), order='F', dtype=np.int32)
    b = np.zeros(shape=(klon+1, klev), order='F', dtype=np.int32)
    fused_function(a=a, b=b, klon=klon, klev=klev)
    assert np.all(a == np.array([list(range(1, klev+2))] * klon, order='F'))
    assert np.all(b == np.array([[jl + jk for jk in range(1, klev+1)]
                                for jl in range(1, klon+2)], order='F'))

    clean_test(filepath)
    clean_test(fused_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fuse_collapse_range(here, frontend):
    fcode = """
subroutine transform_loop_fuse_collapse_range(a, b, klon, klev)
  integer, intent(inout) :: a(klon, klev+1), b(klon+1, klev)
  integer, intent(in) :: klon, klev
  integer :: jk, jl, start = 15

!$loki loop-fusion collapse(2)
  do jk=1,klev+1
    do jl=1,klon
      a(jl, jk) = jk
    end do
  end do

!$loki loop-fusion collapse(2) range(1:1+klev,1:klon+1)
  do jk=start,klev
    do jl=1,klon+1
      b(jl, jk) = jl + jk
    end do
  end do
end subroutine transform_loop_fuse_collapse_range
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    klon, klev = 32, 100
    a = np.zeros(shape=(klon, klev+1), order='F', dtype=np.int32)
    b = np.zeros(shape=(klon+1, klev), order='F', dtype=np.int32)
    function(a=a, b=b, klon=klon, klev=klev)
    assert np.all(a == np.array([list(range(1, klev+2))] * klon, order='F'))
    assert np.all(b[..., 14:] == np.array([[jl + jk for jk in range(15, klev+1)]
                                           for jl in range(1, klon+2)], order='F'))

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 4
    loop_fusion(routine)
    loops = FindNodes(Loop).visit(routine.body)
    assert len(loops) == 2
    assert all(loop.bounds.start == '1' for loop in loops)
    assert sum([loop.bounds.stop == '1 + klev' for loop in loops]) == 1
    assert sum([loop.bounds.stop == 'klon + 1' for loop in loops]) == 1
    assert len(FindNodes(Conditional).visit(routine.body)) == 2

    fused_filepath = here/('%s_fused_%s.f90' % (routine.name, frontend))
    fused_function = jit_compile(routine, filepath=fused_filepath, objname=routine.name)

    # Test transformation
    klon, klev = 32, 100
    a = np.zeros(shape=(klon, klev+1), order='F', dtype=np.int32)
    b = np.zeros(shape=(klon+1, klev), order='F', dtype=np.int32)
    fused_function(a=a, b=b, klon=klon, klev=klev)
    assert np.all(a == np.array([list(range(1, klev+2))] * klon, order='F'))
    assert np.all(b[..., 14:] == np.array([[jl + jk for jk in range(15, klev+1)]
                                           for jl in range(1, klon+2)], order='F'))

    clean_test(filepath)
    clean_test(fused_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fission_single(here, frontend):
    fcode = """
subroutine transform_loop_fission_single(a, b, n)
  integer, intent(out) :: a(n), b(n)
  integer, intent(in) :: n
  integer :: j

  do j=1,n
    a(j) = j
    !$loki loop-fission
    b(j) = n-j
  end do
end subroutine transform_loop_fission_single
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    n = 100
    a = np.zeros(shape=(n,), dtype=np.int32)
    b = np.zeros(shape=(n,), dtype=np.int32)
    function(a=a, b=b, n=n)
    assert np.all(a == range(1,n+1))
    assert np.all(b == range(n-1, -1, -1))

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 1
    loop_fission(routine)

    loops = FindNodes(Loop).visit(routine.body)
    assert len(loops) == 2
    for loop in loops:
        assert loop.bounds.start == '1'
        assert loop.bounds.stop == 'n'

    fissioned_filepath = here/('%s_fissioned_%s.f90' % (routine.name, frontend))
    fissioned_function = jit_compile(routine, filepath=fissioned_filepath, objname=routine.name)

    # Test transformation
    n = 100
    a = np.zeros(shape=(n,), dtype=np.int32)
    b = np.zeros(shape=(n,), dtype=np.int32)
    fissioned_function(a=a, b=b, n=n)
    assert np.all(a == range(1,n+1))
    assert np.all(b == range(n-1, -1, -1))

    clean_test(filepath)
    clean_test(fissioned_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fission_multiple(here, frontend):
    fcode = """
subroutine transform_loop_fission_multiple(a, b, c, n)
  integer, intent(out) :: a(n), b(n), c(n)
  integer, intent(in) :: n
  integer :: j

  do j=1,n
    a(j) = j
    !$loki loop-fission
    b(j) = n-j
    !$loki loop-fission
    c(j) = a(j) + b(j)
  end do
end subroutine transform_loop_fission_multiple
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    n = 100
    a = np.zeros(shape=(n,), dtype=np.int32)
    b = np.zeros(shape=(n,), dtype=np.int32)
    c = np.zeros(shape=(n,), dtype=np.int32)
    function(a=a, b=b, c=c, n=n)
    assert np.all(a == range(1,n+1))
    assert np.all(b == range(n-1, -1, -1))
    assert np.all(c == n)

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 1
    loop_fission(routine)

    loops = FindNodes(Loop).visit(routine.body)
    assert len(loops) == 3
    for loop in loops:
        assert loop.bounds.start == '1'
        assert loop.bounds.stop == 'n'

    fissioned_filepath = here/('%s_fissioned_%s.f90' % (routine.name, frontend))
    fissioned_function = jit_compile(routine, filepath=fissioned_filepath, objname=routine.name)

    # Test transformation
    n = 100
    a = np.zeros(shape=(n,), dtype=np.int32)
    b = np.zeros(shape=(n,), dtype=np.int32)
    c = np.zeros(shape=(n,), dtype=np.int32)
    fissioned_function(a=a, b=b, c=c, n=n)
    assert np.all(a == range(1,n+1))
    assert np.all(b == range(n-1, -1, -1))
    assert np.all(c == n)

    clean_test(filepath)
    clean_test(fissioned_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fission_promote(here, frontend):
    fcode = """
subroutine transform_loop_fission_promote(a, b, n)
  integer, intent(out) :: a(n), b(n)
  integer, intent(in) :: n
  integer :: j, tmp

  do j=1,n
    a(j) = j
    tmp = j - 1
    !$loki loop-fission promote(tmp)
    b(j) = n-tmp
  end do
end subroutine transform_loop_fission_promote
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    n = 100
    a = np.zeros(shape=(n,), dtype=np.int32)
    b = np.zeros(shape=(n,), dtype=np.int32)
    function(a=a, b=b, n=n)
    assert np.all(a == range(1,n+1))
    assert np.all(b == range(n, 0, -1))

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 1
    loop_fission(routine)

    loops = FindNodes(Loop).visit(routine.body)
    assert len(loops) == 2
    for loop in loops:
        assert loop.bounds.start == '1'
        assert loop.bounds.stop == 'n'
    assert [str(d) for d in routine.variable_map['tmp'].shape] == ['n']

    fissioned_filepath = here/('%s_fissioned_%s.f90' % (routine.name, frontend))
    fissioned_function = jit_compile(routine, filepath=fissioned_filepath, objname=routine.name)

    # Test transformation
    n = 100
    a = np.zeros(shape=(n,), dtype=np.int32)
    b = np.zeros(shape=(n,), dtype=np.int32)
    fissioned_function(a=a, b=b, n=n)
    assert np.all(a == range(1,n+1))
    assert np.all(b == range(n, 0, -1))

    clean_test(filepath)
    clean_test(fissioned_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fission_promote_conflicting_lengths(here, frontend):
    fcode = """
subroutine transform_loop_fission_promote_conflicting_lengths(a, b, n)
  integer, intent(out) :: a(n), b(n+1)
  integer, intent(in) :: n
  integer :: j, tmp

  do j=1,n
    tmp = j - 1
    !$loki loop-fission promote(tmp)
    a(j) = tmp + 1
  end do

  do j=1,n+1
    tmp = j - 1
    !$loki loop-fission promote(tmp)
    b(j) = n-tmp
  end do
end subroutine transform_loop_fission_promote_conflicting_lengths
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    n = 100
    a = np.zeros(shape=(n,), dtype=np.int32)
    b = np.zeros(shape=(n+1,), dtype=np.int32)
    function(a=a, b=b, n=n)
    assert np.all(a == range(1,n+1))
    assert np.all(b == range(n, -1, -1))

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 2
    loop_fission(routine)

    loops = FindNodes(Loop).visit(routine.body)
    assert len(loops) == 4
    for loop in loops:
        assert loop.bounds.start == '1'
    assert loops[0].bounds.stop == 'n'
    assert loops[1].bounds.stop == 'n'
    assert loops[2].bounds.stop == 'n + 1'
    assert loops[3].bounds.stop == 'n + 1'
    assert [str(d) for d in routine.variable_map['tmp'].shape] == ['1 + n']

    fissioned_filepath = here/('%s_fissioned_%s.f90' % (routine.name, frontend))
    fissioned_function = jit_compile(routine, filepath=fissioned_filepath, objname=routine.name)

    # Test transformation
    n = 100
    a = np.zeros(shape=(n,), dtype=np.int32)
    b = np.zeros(shape=(n+1,), dtype=np.int32)
    fissioned_function(a=a, b=b, n=n)
    assert np.all(a == range(1,n+1))
    assert np.all(b == range(n, -1, -1))

    clean_test(filepath)
    clean_test(fissioned_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fission_promote_array(here, frontend):
    fcode = """
subroutine transform_loop_fission_promote_array(a, klon, klev)
  integer, intent(inout) :: a(klon, klev)
  integer, intent(in) :: klon, klev
  integer :: jk, jl, zsupsat(klon)

  do jk=1,klev
    zsupsat(:) = 0
    do jl=1,klon
        zsupsat(jl) = jl
    end do
    !$loki loop-fission promote(ZSUPSAT)
    a(:, jk) = zsupsat(:)
  end do
end subroutine transform_loop_fission_promote_array
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    klon, klev = 32, 100
    a = np.zeros(shape=(klon, klev), order='F', dtype=np.int32)
    function(a=a, klon=klon, klev=klev)
    assert np.all(a == np.array([[jl] * klev for jl in range(1, klon+1)], order='F'))

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 2
    loop_fission(routine)

    loops = FindNodes(Loop).visit(routine.body)
    assert len(loops) == 3
    assert all(loop.bounds.start == '1' for loop in loops)
    assert sum([loop.bounds.stop == 'klev' for loop in loops]) == 2
    if frontend == OMNI:
        assert [str(d) for d in routine.variable_map['zsupsat'].shape] == ['1:klon', 'klev']
    else:
        assert [str(d) for d in routine.variable_map['zsupsat'].shape] == ['klon', 'klev']

    fissioned_filepath = here/('%s_fissioned_%s.f90' % (routine.name, frontend))
    fissioned_function = jit_compile(routine, filepath=fissioned_filepath, objname=routine.name)

    # Test transformation
    klon, klev = 32, 100
    a = np.zeros(shape=(klon, klev), order='F', dtype=np.int32)
    fissioned_function(a=a, klon=klon, klev=klev)
    assert np.all(a == np.array([[jl] * klev for jl in range(1, klon+1)], order='F'))

    clean_test(filepath)
    clean_test(fissioned_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fission_promote_multiple(here, frontend):
    fcode = """
subroutine transform_loop_fission_promote_multiple(a, klon, klev)
  integer, intent(inout) :: a(klon, klev)
  integer, intent(in) :: klon, klev
  integer :: jk, jl, zsupsat(klon), tmp

  do jk=1,klev
    zsupsat(:) = 0
    do jl=1,klon
        zsupsat(jl) = jl
    end do
    tmp = jk
    !$loki loop-fission promote(ZSUPSAT, tmp)
    a(:, jk) = zsupsat(:) + tmp
  end do
end subroutine transform_loop_fission_promote_multiple
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    klon, klev = 32, 100
    a = np.zeros(shape=(klon, klev), order='F', dtype=np.int32)
    function(a=a, klon=klon, klev=klev)
    assert np.all(a == np.array([[jl + jk for jk in range(1, klev+1)]
                                for jl in range(1, klon+1)], order='F'))

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 2
    loop_fission(routine)

    loops = FindNodes(Loop).visit(routine.body)
    assert len(loops) == 3
    assert all(loop.bounds.start == '1' for loop in loops)
    assert sum([loop.bounds.stop == 'klev' for loop in loops]) == 2
    if frontend == OMNI:
        assert [str(d) for d in routine.variable_map['zsupsat'].shape] == ['1:klon', 'klev']
    else:
        assert [str(d) for d in routine.variable_map['zsupsat'].shape] == ['klon', 'klev']
    assert [str(d) for d in routine.variable_map['tmp'].shape] == ['klev']

    fissioned_filepath = here/('%s_fissioned_%s.f90' % (routine.name, frontend))
    fissioned_function = jit_compile(routine, filepath=fissioned_filepath, objname=routine.name)

    # Test transformation
    klon, klev = 32, 100
    a = np.zeros(shape=(klon, klev), order='F', dtype=np.int32)
    fissioned_function(a=a, klon=klon, klev=klev)
    assert np.all(a == np.array([[jl + jk for jk in range(1, klev+1)]
                                for jl in range(1, klon+1)], order='F'))

    clean_test(filepath)
    clean_test(fissioned_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fission_multiple_promote(here, frontend):
    fcode = """
subroutine transform_loop_fission_multiple_promote(a, b, klon, klev, nclv)
  integer, intent(inout) :: a(klon, klev), b(klon, klev, nclv)
  integer, intent(in) :: klon, klev, nclv
  integer :: jm, jk, jl, zsupsat(klon), zqxn(klon, nclv)

  do jk=1,klev
    zsupsat(:) = 0
    do jl=1,klon
        zsupsat(jl) = jl
    end do
    !$loki loop-fission
    do jm=1,nclv
        do jl=1,klon
            zqxn(jl, jm) = jm+jl
        end do
    end do
    !$loki loop-fission promote(ZSUPSAT)
    a(:, jk) = zsupsat(:)
    !$loki loop-fission promote( zQxN )
    do jm=1,nclv
        b(:, jk, jm) = zqxn(:, jm)
    end do
  end do
end subroutine transform_loop_fission_multiple_promote
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    klon, klev, nclv = 32, 100, 5
    a = np.zeros(shape=(klon, klev), order='F', dtype=np.int32)
    b = np.zeros(shape=(klon, klev, nclv), order='F', dtype=np.int32)
    function(a=a, b=b, klon=klon, klev=klev, nclv=nclv)
    assert np.all(a == np.array([[jl] * klev for jl in range(1, klon+1)], order='F'))
    assert np.all(b == np.array([[[jl + jm for jm in range(1, nclv+1)]] * klev
                                for jl in range(1, klon+1)], order='F'))

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 5
    loop_fission(routine)

    loops = FindNodes(Loop).visit(routine.body)
    assert len(loops) == 8
    assert all(loop.bounds.start == '1' for loop in loops)
    assert sum([loop.bounds.stop == 'klev' for loop in loops]) == 4
    assert sum([loop.bounds.stop == 'klon' for loop in loops]) == 2
    assert sum([loop.bounds.stop == 'nclv' for loop in loops]) == 2
    if frontend == OMNI:
        assert [str(d) for d in routine.variable_map['zsupsat'].shape] == ['1:klon', 'klev']
        assert [str(d) for d in routine.variable_map['zqxn'].shape] == ['1:klon', '1:nclv', 'klev']
    else:
        assert [str(d) for d in routine.variable_map['zsupsat'].shape] == ['klon', 'klev']
        assert [str(d) for d in routine.variable_map['zqxn'].shape] == ['klon', 'nclv', 'klev']

    fissioned_filepath = here/('%s_fissioned_%s.f90' % (routine.name, frontend))
    fissioned_function = jit_compile(routine, filepath=fissioned_filepath, objname=routine.name)

    # Test transformation
    klon, klev, nclv = 32, 100, 5
    a = np.zeros(shape=(klon, klev), order='F', dtype=np.int32)
    b = np.zeros(shape=(klon, klev, nclv), order='F', dtype=np.int32)
    fissioned_function(a=a, b=b, klon=klon, klev=klev, nclv=nclv)
    assert np.all(a == np.array([[jl] * klev for jl in range(1, klon+1)], order='F'))
    assert np.all(b == np.array([[[jl + jm for jm in range(1, nclv+1)]] * klev
                                for jl in range(1, klon+1)], order='F'))

    clean_test(filepath)
    clean_test(fissioned_filepath)


@pytest.mark.parametrize('frontend', [OFP, OMNI, FP])
def test_transform_loop_fusion_fission(here, frontend):
    fcode = """
subroutine transform_loop_fusion_fission(a, b, klon, klev)
  integer, intent(inout) :: a(klon, klev), b(klon, klev)
  integer, intent(in) :: klon, klev
  integer :: jk, jl, zsupsat(klon)

!$loki loop-fusion
  do jk=1,klev
    do jl=1,klon
      a(jl, jk) = jk
    end do
  end do

!$loki loop-fusion
  do jk=1,klev
    do jl=1,klon
      zsupsat(jl) = jl
    end do
    !$loki loop-fission promote(zsupsat)
    b(:, jk) = a(:, jk) + zsupsat(:)
  end do
end subroutine transform_loop_fusion_fission
"""
    routine = Subroutine.from_source(fcode, frontend=frontend)
    filepath = here/('%s_%s.f90' % (routine.name, frontend))
    function = jit_compile(routine, filepath=filepath, objname=routine.name)

    # Test the reference solution
    klon, klev = 32, 100
    a = np.zeros(shape=(klon, klev), order='F', dtype=np.int32)
    b = np.zeros(shape=(klon, klev), order='F', dtype=np.int32)
    function(a=a, b=b, klon=klon, klev=klev)
    assert np.all(a == np.array([list(range(1, klev+1))] * klon, order='F'))
    assert np.all(b == np.array([[jl + jk for jk in range(1, klev+1)]
                                for jl in range(1, klon+1)], order='F'))

    # Apply transformation
    assert len(FindNodes(Loop).visit(routine.body)) == 4
    loop_fusion(routine)
    assert len(FindNodes(Loop).visit(routine.body)) == 3
    loop_fission(routine)

    loops = FindNodes(Loop).visit(routine.body)
    assert len(loops) == 4
    assert all(loop.bounds.start == '1' for loop in loops)
    assert sum([loop.bounds.stop == 'klev' for loop in loops]) == 2
    assert sum([loop.bounds.stop == 'klon' for loop in loops]) == 2
    if frontend == OMNI:
        assert [str(d) for d in routine.variable_map['zsupsat'].shape] == ['1:klon', 'klev']
    else:
        assert [str(d) for d in routine.variable_map['zsupsat'].shape] == ['klon', 'klev']

    fissioned_filepath = here/('%s_fissioned_%s.f90' % (routine.name, frontend))
    fissioned_function = jit_compile(routine, filepath=fissioned_filepath, objname=routine.name)

    # Test transformation
    klon, klev = 32, 100
    a = np.zeros(shape=(klon, klev), order='F', dtype=np.int32)
    b = np.zeros(shape=(klon, klev), order='F', dtype=np.int32)
    fissioned_function(a=a, b=b, klon=klon, klev=klev)
    assert np.all(a == np.array([list(range(1, klev+1))] * klon, order='F'))
    assert np.all(b == np.array([[jl + jk for jk in range(1, klev+1)]
                                for jl in range(1, klon+1)], order='F'))

    clean_test(filepath)
    clean_test(fissioned_filepath)