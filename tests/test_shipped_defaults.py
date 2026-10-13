"""The shipped defaults must be the configuration that was actually measured.

A default `strandline.run` invocation and a dev-event score have to describe
the same detector, or the project cannot say what its own accuracy is: the
published rows would belong to a configuration no user ever runs, and a user's
run would carry a `params_hash` matching no recorded score.

That is not hypothetical. Until 2026-10-13 `RunParams.n_pre` was 5 while every
scored row used `DEV_N_PRE = 3`, so a default run produced an unmeasured
configuration with an unpublished hash. `docs/accuracy_diagnosis.md` measured
the mismatch and `config.N_PRE` closed it. These tests keep it closed.

They deliberately assert a RELATIONSHIP, not a number: a human may change
`N_PRE` (and re-score), and nothing here should have to be edited to allow it.
Pinning the literal 3 would make this a second place to update and so a second
place to forget.
"""

from dataclasses import replace

from eval.fetch_dev_data import dev_run_params
from strandline.config import DEV_N_PRE, N_PRE, DetectParams, OpticalParams, RunParams
from strandline.run import params_hash


def test_the_shipped_stack_depth_is_the_one_the_dev_set_was_scored_at():
    assert RunParams().n_pre == DEV_N_PRE, (
        f"a default run uses {RunParams().n_pre} pre-event images but the dev set was "
        f"scored at {DEV_N_PRE}: the shipped configuration is unmeasured")


def test_dev_n_pre_is_the_same_declaration_and_cannot_drift():
    """One value, one place. DEV_N_PRE is an alias, not a second opinion."""
    assert DEV_N_PRE is N_PRE
    assert RunParams().n_pre is N_PRE


def test_the_shipped_stack_is_at_least_the_minimum_the_pipeline_accepts():
    """Bringing the default DOWN must not take it below what catalog refuses on."""
    rp = RunParams()
    assert rp.n_pre >= rp.min_pre, (
        f"n_pre {rp.n_pre} is below min_pre {rp.min_pre}: every run would refuse")


def test_a_default_run_reproduces_the_evaluated_params_hash():
    """The consistency this change exists for, stated as the hash itself.

    `eval.fetch_dev_data.dev_run_params` is what every scored row was produced
    with. A default `RunParams()` must hash identically to it, radar-only and
    with optical, or a user's run_report.json cites a configuration no score
    file exists for.
    """
    dp = DetectParams()
    evaluated = dev_run_params()
    shipped = RunParams()
    assert params_hash(dp, shipped) == params_hash(dp, evaluated)
    op = OpticalParams()
    assert params_hash(dp, shipped, op) == params_hash(dp, evaluated, op)


def test_the_cli_default_is_the_shipped_default():
    """`--n-pre` must not quietly offer a third value."""
    import strandline.run as pipeline

    parser_src = (pipeline.__file__ and open(pipeline.__file__, encoding="utf-8").read())
    assert 'ap.add_argument("--n-pre", type=int, default=RunParams.n_pre)' in parser_src, (
        "the CLI's --n-pre default must come from RunParams.n_pre, not a literal")


def test_changing_the_stack_depth_is_a_different_configuration():
    """The guard that makes the equality above meaningful rather than vacuous.

    If n_pre did not reach params_hash at all, every test here would pass while
    stack depth silently stopped being part of a row's identity.
    """
    dp = RunParams()
    deeper = replace(dp, n_pre=dp.n_pre + 2)
    assert params_hash(DetectParams(), deeper) != params_hash(DetectParams(), dp)
