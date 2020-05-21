from pathlib import Path
import pytest

from conftest import generate_report_handler, generate_linter
from loki.lint.rules import LimitSubroutineStatementsRule
from loki.frontend import FP


@pytest.fixture(scope='module', name='refpath')
def fixture_refpath():
    return Path(__file__).parent / 'limit_subroutine_statements.f90'


@pytest.mark.parametrize('frontend, max_num_statements, passes', [
    (FP, 10, True),
    (FP, 4, True),
    (FP, 3, False)])
def test_limit_subroutine_stmts(refpath, frontend, max_num_statements, passes):
    '''Test for different maximum allowed number of executable statements and
    content of messages generated by LimitSubroutineStatementsRule.'''
    handler = generate_report_handler()
    config = {'LimitSubroutineStatementsRule': {'max_num_statements': max_num_statements}}
    _ = generate_linter(refpath, [LimitSubroutineStatementsRule], config=config,
                        frontend=frontend, handlers=[handler])

    assert len(handler.target.messages) == 0 if passes else 1
    assert all(all(keyword in msg for keyword in (
        'LimitSubroutineStatementsRule', '[2.2]', '4', str(max_num_statements),
        'routine_limit_statements')) for msg in handler.target.messages)
