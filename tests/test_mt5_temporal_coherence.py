"""ATLAS-MT5-ACCOUNTING-06J: tests offline del evaluador temporal puro.

Sin MT5, sin terminal, sin red, sin credenciales y sin reloj real. Toda la
evidencia (offset, flags, ventanas monotonicas) es sintetica e inyectada.
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as datetime_module
import inspect
import time as time_module
from datetime import datetime, timedelta, timezone

import pytest

import execution.mt5_temporal_coherence as temporal
from execution.mt5_temporal_coherence import (
    ClockOffsetEstimate,
    MT5AccountMarkTemporalResult,
    MT5ClockOffsetValidation,
    MT5MarkSetTemporalResult,
    MT5MarkTemporalResult,
    MT5SnapshotTemporalResult,
    MT5TemporalCoherenceError,
    MT5TemporalCoherenceResult,
    MT5TemporalDimensionStatus,
    MT5TemporalInput,
    MT5TemporalMarkInput,
    MT5TemporalOverallStatus,
    MT5TemporalPolicy,
    MT5TemporalScope,
    evaluate_mt5_temporal_coherence,
)

UTC = timezone.utc
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
D = MT5TemporalDimensionStatus
S = MT5TemporalScope
O = MT5TemporalOverallStatus


def dt(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


def _policy(**over) -> MT5TemporalPolicy:
    base = dict(
        max_mark_age_s=5.0,
        max_mark_skew_s=2.0,
        max_snapshot_skew_s=1.0,
        max_snapshot_age_s=5.0,
        max_clock_offset_uncertainty_s=0.5,
        max_read_span_s=1.0,
        max_receive_delay_s=2.0,
        max_receive_age_s=5.0,
    )
    base.update(over)
    return MT5TemporalPolicy(**base)


def _offset(**over) -> ClockOffsetEstimate:
    base = dict(
        offset_seconds=0.0,
        uncertainty_seconds=0.1,
        measured_at=NOW,
        valid_from=dt(-60),
        valid_to=dt(60),
        clock_domain_id="TICK-A",
        local_clock_domain_id="LOCAL-A",
        source_id="source-1",
        provenance="synthetic",
        evidence_ref="evidence-1",
        tick_time_domain_confirmed=True,
    )
    base.update(over)
    return ClockOffsetEstimate(**base)


def _mark(**over) -> MT5TemporalMarkInput:
    base = dict(
        position_id="P1",
        atlas_instrument="EURUSD",
        mt5_symbol="EURUSD.raw",
        tick_time=dt(-1),
        received_at=dt(-0.5),
        clock_domain_id="TICK-A",
        tick_time_domain_confirmed=True,
        tick_flags=3,
        bid_flag=1,
        ask_flag=2,
        flag_semantics_confirmed=True,
        required_sides=("BID",),
        read_started_monotonic=100.0,
        read_finished_monotonic=100.1,
    )
    base.update(over)
    return MT5TemporalMarkInput(**base)


def _input(**over) -> MT5TemporalInput:
    base = dict(
        scope=S.MARK_ONLY,
        valuation_as_of=NOW,
        local_clock_domain_id="LOCAL-A",
        clock_offset_estimate=_offset(),
        marks=(_mark(),),
        snapshot_observed_at=None,
        snapshot_read_started_monotonic=None,
        snapshot_read_finished_monotonic=None,
    )
    base.update(over)
    return MT5TemporalInput(**base)


def _account_input(**over) -> MT5TemporalInput:
    base = dict(
        scope=S.ACCOUNT_AND_MARKS,
        valuation_as_of=NOW,
        local_clock_domain_id="LOCAL-A",
        clock_offset_estimate=_offset(),
        marks=(_mark(),),
        snapshot_observed_at=dt(-0.5),
        snapshot_read_started_monotonic=200.0,
        snapshot_read_finished_monotonic=200.1,
    )
    base.update(over)
    return MT5TemporalInput(**base)


def _eval(data=None, policy=None) -> MT5TemporalCoherenceResult:
    return evaluate_mt5_temporal_coherence(
        data if data is not None else _input(),
        policy if policy is not None else _policy(),
    )


class TestPublicContract:
    def test_requires_input_instance(self):
        with pytest.raises(MT5TemporalCoherenceError):
            evaluate_mt5_temporal_coherence("x", _policy())

    def test_requires_policy_instance(self):
        with pytest.raises(MT5TemporalCoherenceError):
            evaluate_mt5_temporal_coherence(_input(), "x")

    def test_invalid_scope_is_contract_error(self):
        data = _input()
        object.__setattr__(data, "scope", "MARK_ONLY")
        with pytest.raises(MT5TemporalCoherenceError):
            _eval(data)

    def test_frozen_offset(self):
        offset = _offset()
        with pytest.raises(dataclasses.FrozenInstanceError):
            offset.offset_seconds = 1.0  # type: ignore[misc]

    def test_frozen_policy(self):
        policy = _policy()
        with pytest.raises(dataclasses.FrozenInstanceError):
            policy.max_mark_age_s = 1.0  # type: ignore[misc]


class TestGlobalTablesImmutable:
    def test_reason_status_is_immutable(self):
        with pytest.raises(TypeError):
            temporal._REASON_STATUS["X"] = D.VERIFIED  # type: ignore[index]

    def test_status_severity_is_immutable(self):
        with pytest.raises(TypeError):
            temporal._STATUS_SEVERITY[D.VERIFIED] = 99  # type: ignore[index]

    def test_reason_messages_is_immutable(self):
        with pytest.raises(TypeError):
            temporal.MT5_TEMPORAL_REASON_MESSAGES["X"] = "x"  # type: ignore[index]

    def test_evaluation_is_deterministic_after_mutation_attempts(self):
        with pytest.raises(TypeError):
            temporal._REASON_STATUS["X"] = D.VERIFIED  # type: ignore[index]
        assert temporal._REASON_STATUS["TICK_TIME_MISSING"] is D.MISSING
        assert _eval() == _eval()


class TestVerifiedSynthetic:
    def test_valid_mark_only_is_verified(self):
        res = _eval()
        assert res.overall is O.TEMPORALLY_VERIFIED
        assert res.reason_codes == ()
        assert res.reason_code is None
        assert res.config_status is D.VERIFIED
        assert res.offset.status is D.VERIFIED
        assert res.markset.status is D.NOT_REQUIRED
        assert res.snapshot.status is D.NOT_REQUIRED
        assert res.account_mark_coherence.status is D.NOT_REQUIRED
        assert len(res.markset.marks) == 1
        mark = res.markset.marks[0]
        assert mark.status is D.VERIFIED
        assert mark.reason_codes == ()
        assert mark.age_estimate_s == pytest.approx(1.0)
        assert mark.age_interval_s == pytest.approx((0.9, 1.1))
        assert mark.receive_delay_estimate_s == pytest.approx(0.5)
        assert mark.receive_delay_interval_s == pytest.approx((0.4, 0.6))
        assert mark.receive_age_s == pytest.approx(0.5)
        assert mark.read_span_s == pytest.approx(0.1)
        assert mark.side_statuses == (("BID", D.VERIFIED),)
        assert mark.corrected_tick_time == dt(-1)

    def test_positive_offset_sign(self):
        data = _input(
            clock_offset_estimate=_offset(
                offset_seconds=2.0, uncertainty_seconds=0.0
            )
        )
        res = _eval(data, _policy(max_receive_delay_s=5.0))
        mark = res.markset.marks[0]
        assert res.overall is O.TEMPORALLY_VERIFIED
        assert mark.age_estimate_s == pytest.approx(3.0)
        assert mark.corrected_tick_time == dt(-3)
        assert mark.receive_delay_estimate_s == pytest.approx(2.5)

    def test_negative_offset_sign(self):
        data = _input(
            clock_offset_estimate=_offset(
                offset_seconds=-2.0, uncertainty_seconds=0.0
            ),
            marks=(_mark(tick_time=dt(-4), received_at=dt(-1)),),
        )
        res = _eval(data)
        mark = res.markset.marks[0]
        assert res.overall is O.TEMPORALLY_VERIFIED
        assert mark.age_estimate_s == pytest.approx(2.0)
        assert mark.corrected_tick_time == dt(-2)
        assert mark.receive_delay_estimate_s == pytest.approx(1.0)

    def test_deterministic_same_inputs(self):
        assert _eval() == _eval()

    def test_no_wall_clock_calls(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise AssertionError("reloj real no permitido")

        class _Meta(type):
            def __instancecheck__(cls, instance):
                return isinstance(instance, datetime_module.datetime)

        class _NoClockDatetime(
            datetime_module.datetime, metaclass=_Meta
        ):
            @classmethod
            def now(cls, tz=None):
                boom()

            @classmethod
            def utcnow(cls):
                boom()

        monkeypatch.setattr(temporal, "datetime", _NoClockDatetime)
        monkeypatch.setattr(time_module, "time", boom)
        assert _eval().overall is O.TEMPORALLY_VERIFIED


class TestOffsetValidation:
    def test_missing_offset(self):
        res = _eval(_input(clock_offset_estimate=None))
        assert res.offset.status is D.MISSING
        assert res.offset.reason_codes == ("CLOCK_OFFSET_MISSING",)
        assert res.overall is O.TEMPORALLY_UNVERIFIED
        assert "CLOCK_OFFSET_MISSING" in res.reason_codes
        assert "CLOCK_SKEW_UNVERIFIED" in res.reason_codes
        assert res.markset.marks[0].status is D.UNVERIFIED

    @pytest.mark.parametrize(
        "over",
        [
            {"offset_seconds": True},
            {"offset_seconds": "1"},
            {"uncertainty_seconds": -1.0},
            {"uncertainty_seconds": float("nan")},
            {"measured_at": datetime(2026, 1, 1, 12, 0, 0)},
            {"valid_from": dt(10), "valid_to": dt(20)},
            {"valid_from": dt(0), "valid_to": dt(0)},
            {"source_id": " "},
            {"tick_time_domain_confirmed": 1},
        ],
    )
    def test_invalid_offset_structure(self, over):
        res = _eval(
            _input(clock_offset_estimate=_offset(**over))
        )
        assert res.offset.status is D.INVALID
        assert "CLOCK_OFFSET_INVALID" in res.offset.reason_codes
        assert res.overall is O.TEMPORALLY_UNVERIFIED

    def test_offset_stale_by_valuation(self):
        offset = _offset(
            measured_at=dt(-90),
            valid_from=dt(-120),
            valid_to=dt(-60),
        )
        res = _eval(_input(clock_offset_estimate=offset))
        assert res.offset.status is D.UNVERIFIED
        assert "CLOCK_OFFSET_STALE" in res.offset.reason_codes

    def test_offset_stale_by_corrected_tick(self):
        offset = _offset(
            valid_from=dt(-2), valid_to=dt(60), measured_at=dt(0)
        )
        res = _eval(
            _input(
                clock_offset_estimate=offset,
                marks=(_mark(tick_time=dt(-10), received_at=dt(-0.5)),),
            )
        )
        assert res.offset.status is D.UNVERIFIED
        assert "CLOCK_OFFSET_STALE" in res.offset.reason_codes

    def test_offset_uncertainty_exceeded(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=1.0)
            )
        )
        assert res.offset.status is D.EXCEEDED
        assert (
            "CLOCK_OFFSET_UNCERTAINTY_EXCEEDED"
            in res.offset.reason_codes
        )

    def test_offset_uncertainty_exact_limit_passes(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.5)
            )
        )
        assert res.offset.status is D.VERIFIED
        assert res.overall is O.TEMPORALLY_VERIFIED

    @pytest.mark.parametrize("measured_at", [dt(-60), dt(60)])
    def test_offset_validity_bounds_are_inclusive(self, measured_at):
        # La vigencia es inclusiva en ambos extremos. La valoracion coincide
        # con measured_at para no introducir look-ahead.
        res = _eval(
            _input(
                valuation_as_of=measured_at,
                clock_offset_estimate=_offset(
                    uncertainty_seconds=0.0,
                    measured_at=measured_at,
                    valid_from=dt(-60),
                    valid_to=dt(60),
                ),
                marks=(
                    _mark(
                        tick_time=measured_at,
                        received_at=measured_at,
                    ),
                ),
            )
        )
        assert res.offset.status is D.VERIFIED
        assert res.overall is O.TEMPORALLY_VERIFIED

    def test_offset_measured_at_valuation_is_allowed(self):
        res = _eval(
            _input(
                valuation_as_of=NOW,
                clock_offset_estimate=_offset(measured_at=NOW),
            )
        )
        assert res.offset.status is D.VERIFIED
        assert res.overall is O.TEMPORALLY_VERIFIED

    def test_offset_measured_after_valuation_is_rejected(self):
        res = _eval(
            _input(
                valuation_as_of=NOW,
                clock_offset_estimate=_offset(measured_at=dt(1)),
            )
        )
        assert res.offset.status is D.UNVERIFIED
        assert (
            "CLOCK_OFFSET_MEASURED_AFTER_VALUATION"
            in res.offset.reason_codes
        )
        assert res.overall is O.TEMPORALLY_UNVERIFIED

    def test_offset_measured_after_valuation_blocks_account_scope(self):
        res = _eval(
            _account_input(
                clock_offset_estimate=_offset(measured_at=dt(1)),
            )
        )
        assert res.offset.status is D.UNVERIFIED
        assert (
            "CLOCK_OFFSET_MEASURED_AFTER_VALUATION"
            in res.reason_codes
        )
        assert res.overall is O.TEMPORALLY_UNVERIFIED

    def test_valuation_at_validity_end_is_not_stale(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(
                    measured_at=NOW,
                    valid_from=dt(-60),
                    valid_to=NOW,
                ),
                valuation_as_of=NOW,
            )
        )
        assert res.offset.status is D.VERIFIED

    def test_local_domain_mismatch(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(
                    local_clock_domain_id="LOCAL-B"
                )
            )
        )
        assert res.offset.status is D.INVALID
        assert "CLOCK_DOMAIN_MISMATCH" in res.offset.reason_codes

    def test_tick_domain_mismatch(self):
        res = _eval(
            _input(marks=(_mark(clock_domain_id="TICK-B"),))
        )
        assert res.offset.status is D.INVALID
        assert "CLOCK_DOMAIN_MISMATCH" in res.offset.reason_codes

    def test_offset_tick_domain_not_confirmed(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(
                    tick_time_domain_confirmed=False
                )
            )
        )
        assert res.offset.status is D.UNVERIFIED
        assert "CLOCK_SKEW_UNVERIFIED" in res.offset.reason_codes

    def test_mark_tick_domain_not_confirmed(self):
        res = _eval(
            _input(
                marks=(
                    _mark(
                        tick_time_domain_confirmed=False,
                    ),
                )
            )
        )
        assert "CLOCK_SKEW_UNVERIFIED" in res.offset.reason_codes

    def test_missing_local_domain_prevents_verification(self):
        res = _eval(_input(local_clock_domain_id=None))
        assert res.overall is O.TEMPORALLY_UNVERIFIED
        assert "CLOCK_SKEW_UNVERIFIED" in res.reason_codes


class TestMarketAge:
    def test_exact_max_age_passes(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.0),
                marks=(_mark(tick_time=dt(-5), received_at=dt(-0.5)),),
            ),
            _policy(max_receive_delay_s=10.0),
        )
        mark = res.markset.marks[0]
        assert mark.age_interval_s == pytest.approx((5.0, 5.0))
        assert mark.status is D.VERIFIED
        assert res.overall is O.TEMPORALLY_VERIFIED

    def test_inside_max_age_passes(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.0),
                marks=(_mark(tick_time=dt(-4.999), received_at=dt(-0.5)),),
            ),
            _policy(max_receive_delay_s=10.0),
        )
        assert res.markset.marks[0].status is D.VERIFIED

    def test_outside_max_age(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.0),
                marks=(_mark(tick_time=dt(-5.001), received_at=dt(-0.5)),),
            ),
            _policy(max_receive_delay_s=10.0),
        )
        mark = res.markset.marks[0]
        assert mark.status is D.EXCEEDED
        assert "TICK_TOO_OLD" in mark.reason_codes

    def test_certifiably_future_tick(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.0),
                marks=(_mark(tick_time=dt(1), received_at=dt(-0.5)),),
            )
        )
        mark = res.markset.marks[0]
        assert mark.status is D.INVALID
        assert "TICK_TIME_FUTURE" in mark.reason_codes

    def test_age_interval_crosses_zero(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=2.0),
                marks=(_mark(tick_time=dt(-1), received_at=dt(-0.5)),),
            ),
            _policy(
                max_clock_offset_uncertainty_s=3.0,
                max_receive_delay_s=10.0,
            ),
        )
        mark = res.markset.marks[0]
        assert mark.status is D.UNVERIFIED
        assert "MARK_AGE_UNVERIFIED" in mark.reason_codes
        assert mark.age_interval_s == pytest.approx((-1.0, 3.0))

    def test_age_interval_crossing_max_is_unverified(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.5),
                marks=(
                    _mark(tick_time=dt(-4.8), received_at=dt(-0.5)),
                ),
            ),
            _policy(max_receive_delay_s=10.0),
        )
        mark = res.markset.marks[0]
        assert mark.age_interval_s == pytest.approx((4.3, 5.3))
        assert mark.status is D.UNVERIFIED
        assert "MARK_AGE_UNVERIFIED" in mark.reason_codes
        assert "TICK_TOO_OLD" not in mark.reason_codes

    def test_age_interval_with_upper_bound_exact_is_verified(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.2),
                marks=(
                    _mark(tick_time=dt(-4.8), received_at=dt(-0.5)),
                ),
            ),
            _policy(max_receive_delay_s=10.0),
        )
        mark = res.markset.marks[0]
        assert mark.age_interval_s == pytest.approx((4.6, 5.0))
        assert mark.status is D.VERIFIED
        assert res.overall is O.TEMPORALLY_VERIFIED

    def test_age_interval_fully_above_max_is_exceeded(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.2),
                marks=(
                    _mark(tick_time=dt(-5.3), received_at=dt(-0.5)),
                ),
            ),
            _policy(max_receive_delay_s=10.0),
        )
        mark = res.markset.marks[0]
        assert mark.age_interval_s == pytest.approx((5.1, 5.5))
        assert mark.status is D.EXCEEDED
        assert "TICK_TOO_OLD" in mark.reason_codes

    def test_old_tick_received_recently(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.0),
                marks=(_mark(tick_time=dt(-10), received_at=dt(-0.5)),),
            ),
            _policy(max_receive_delay_s=10.0),
        )
        mark = res.markset.marks[0]
        assert mark.status is D.EXCEEDED
        assert "TICK_TOO_OLD" in mark.reason_codes
        assert mark.receive_age_s == pytest.approx(0.5)


class TestReceiveDelayAndAge:
    def test_delay_exact_max_passes(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.0),
                marks=(_mark(tick_time=dt(-3), received_at=dt(-1)),),
            )
        )
        mark = res.markset.marks[0]
        assert mark.receive_delay_interval_s == pytest.approx((2.0, 2.0))
        assert mark.status is D.VERIFIED

    def test_delay_exceeded(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.0),
                marks=(_mark(tick_time=dt(-3), received_at=dt(-0.5)),),
            )
        )
        mark = res.markset.marks[0]
        assert mark.status is D.EXCEEDED
        assert "RECEIVE_DELAY_EXCEEDED" in mark.reason_codes

    def test_delay_negative_is_unverified(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.0),
                marks=(_mark(tick_time=dt(-1), received_at=dt(-5)),),
            )
        )
        mark = res.markset.marks[0]
        assert mark.status is D.UNVERIFIED
        assert "RECEIVE_DELAY_UNVERIFIED" in mark.reason_codes

    def test_delay_interval_crosses_zero(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.5),
                marks=(_mark(tick_time=dt(-1), received_at=dt(-1)),),
            ),
            _policy(max_receive_delay_s=10.0),
        )
        mark = res.markset.marks[0]
        assert mark.status is D.UNVERIFIED
        assert "RECEIVE_DELAY_UNVERIFIED" in mark.reason_codes

    def test_delay_interval_crossing_max_is_unverified(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.5),
                marks=(_mark(tick_time=dt(-3), received_at=dt(-0.8)),),
            )
        )
        mark = res.markset.marks[0]
        assert mark.receive_delay_interval_s == pytest.approx((1.7, 2.7))
        assert mark.status is D.UNVERIFIED
        assert "RECEIVE_DELAY_UNVERIFIED" in mark.reason_codes
        assert "RECEIVE_DELAY_EXCEEDED" not in mark.reason_codes

    def test_delay_interval_fully_above_max_is_exceeded(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.5),
                marks=(_mark(tick_time=dt(-3), received_at=dt(-0.4)),),
            )
        )
        mark = res.markset.marks[0]
        assert mark.receive_delay_interval_s == pytest.approx((2.1, 3.1))
        assert mark.status is D.EXCEEDED
        assert "RECEIVE_DELAY_EXCEEDED" in mark.reason_codes

    def test_delay_interval_with_upper_bound_exact_is_verified(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.2),
                marks=(_mark(tick_time=dt(-3), received_at=dt(-1.2)),),
            )
        )
        mark = res.markset.marks[0]
        assert mark.receive_delay_interval_s == pytest.approx((1.6, 2.0))
        assert mark.status is D.VERIFIED
        assert res.overall is O.TEMPORALLY_VERIFIED

    def test_receive_age_exact_limit_passes(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.0),
                marks=(_mark(tick_time=dt(-6), received_at=dt(-5)),),
            ),
            _policy(max_mark_age_s=10.0),
        )
        mark = res.markset.marks[0]
        assert mark.receive_age_s == pytest.approx(5.0)
        assert mark.status is D.VERIFIED
        assert res.overall is O.TEMPORALLY_VERIFIED

    def test_receive_age_exceeded(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.0),
                marks=(_mark(tick_time=dt(-1), received_at=dt(-6)),),
            ),
            _policy(max_receive_delay_s=10.0),
        )
        mark = res.markset.marks[0]
        assert mark.status is D.EXCEEDED
        assert "RECEIVE_AGE_EXCEEDED" in mark.reason_codes

    def test_receive_after_valuation_is_invalid(self):
        res = _eval(
            _input(
                marks=(_mark(received_at=dt(1)),),
            )
        )
        mark = res.markset.marks[0]
        assert mark.status is D.INVALID
        assert "RECEIVE_AGE_INVALID" in mark.reason_codes


class TestTimestamps:
    def test_tick_time_missing_is_not_substituted(self):
        res = _eval(_input(marks=(_mark(tick_time=None),)))
        mark = res.markset.marks[0]
        assert mark.status is D.MISSING
        assert mark.tick_time is None
        assert "TICK_TIME_MISSING" in mark.reason_codes
        assert mark.received_at is not None

    def test_tick_time_naive_is_invalid(self):
        res = _eval(
            _input(
                marks=(
                    _mark(tick_time=datetime(2026, 1, 1, 11, 59, 59)),
                )
            )
        )
        mark = res.markset.marks[0]
        assert mark.status is D.INVALID
        assert "TICK_TIME_INVALID" in mark.reason_codes

    def test_tick_time_wrong_type_is_invalid(self):
        res = _eval(_input(marks=(_mark(tick_time=123),)))
        mark = res.markset.marks[0]
        assert mark.status is D.INVALID
        assert "TICK_TIME_INVALID" in mark.reason_codes

    def test_received_at_missing(self):
        res = _eval(_input(marks=(_mark(received_at=None),)))
        mark = res.markset.marks[0]
        assert mark.status is D.MISSING
        assert "RECEIVED_AT_MISSING" in mark.reason_codes

    def test_received_at_naive(self):
        res = _eval(
            _input(
                marks=(
                    _mark(received_at=datetime(2026, 1, 1, 11, 59, 59)),
                )
            )
        )
        mark = res.markset.marks[0]
        assert mark.status is D.INVALID
        assert "RECEIVED_AT_INVALID" in mark.reason_codes

    def test_valuation_missing_blocks(self):
        res = _eval(_input(valuation_as_of=None))
        assert res.overall is O.TEMPORAL_CHECK_BLOCKED
        assert res.reason_codes == ("VALUATION_AS_OF_MISSING",)
        assert res.config_status is D.VERIFIED

    def test_valuation_naive_blocks(self):
        res = _eval(
            _input(valuation_as_of=datetime(2026, 1, 1, 12, 0, 0))
        )
        assert res.overall is O.TEMPORAL_CHECK_BLOCKED
        assert res.reason_codes == ("VALUATION_AS_OF_INVALID",)


class TestSideFreshness:
    def test_flags_missing(self):
        res = _eval(_input(marks=(_mark(tick_flags=None),)))
        mark = res.markset.marks[0]
        assert mark.status is D.MISSING
        assert "MARK_FLAGS_MISSING" in mark.reason_codes
        assert mark.side_statuses == (("BID", D.MISSING),)

    def test_public_mask_missing(self):
        res = _eval(_input(marks=(_mark(bid_flag=None),)))
        mark = res.markset.marks[0]
        assert mark.status is D.MISSING
        assert "MARK_FLAGS_MISSING" in mark.reason_codes

    def test_boolean_mask_is_invalid(self):
        res = _eval(_input(marks=(_mark(bid_flag=True),)))
        mark = res.markset.marks[0]
        assert "MARK_FLAGS_MISSING" in mark.reason_codes

    def test_boolean_tick_flags_is_invalid(self):
        res = _eval(_input(marks=(_mark(tick_flags=True),)))
        mark = res.markset.marks[0]
        assert "MARK_FLAGS_MISSING" in mark.reason_codes

    def test_side_bit_not_updated(self):
        res = _eval(
            _input(marks=(_mark(tick_flags=2, bid_flag=1, ask_flag=2),))
        )
        mark = res.markset.marks[0]
        assert mark.status is D.UNVERIFIED
        assert "MARK_SIDE_UNVERIFIED" in mark.reason_codes
        assert mark.side_statuses == (("BID", D.UNVERIFIED),)

    def test_flag_semantics_not_confirmed(self):
        res = _eval(
            _input(marks=(_mark(flag_semantics_confirmed=False),))
        )
        mark = res.markset.marks[0]
        assert mark.status is D.UNVERIFIED
        assert "MARK_SIDE_UNVERIFIED" in mark.reason_codes

    def test_updated_side_verified_when_semantics_confirmed(self):
        res = _eval(
            _input(
                marks=(
                    _mark(tick_flags=2, required_sides=("ASK",)),
                )
            )
        )
        mark = res.markset.marks[0]
        assert mark.status is D.VERIFIED
        assert mark.side_statuses == (("ASK", D.VERIFIED),)

    def test_no_requested_sides_skips_side_check(self):
        """required_sides=() no solicita frescura individual de bid/ask.

        En ese caso no se evaluan lados, `side_statuses` queda vacio y un
        resultado global verificado NO certifica la frescura de esos lados.
        """
        res = _eval(
            _input(
                marks=(
                    _mark(
                        tick_flags=None,
                        bid_flag=None,
                        ask_flag=None,
                        flag_semantics_confirmed=False,
                        required_sides=(),
                    ),
                )
            )
        )
        mark = res.markset.marks[0]
        assert mark.status is D.VERIFIED
        assert mark.side_statuses == ()

    def test_invalid_requested_side(self):
        res = _eval(
            _input(marks=(_mark(required_sides=("MID",)),))
        )
        mark = res.markset.marks[0]
        assert mark.status is D.UNVERIFIED
        assert "MARK_SIDE_UNVERIFIED" in mark.reason_codes


class TestMarkSet:
    def _two_marks(self, **over):
        first = _mark(
            position_id="P1",
            tick_time=dt(-2),
            received_at=dt(-1),
        )
        second = _mark(
            position_id="P2",
            tick_time=dt(-1),
            received_at=dt(-0.5),
        )
        return _input(
            scope=S.MARK_SET,
            marks=(first, second),
            **over,
        )

    def test_valid_mark_set(self):
        res = _eval(self._two_marks())
        assert res.overall is O.TEMPORALLY_VERIFIED
        assert res.markset.status is D.VERIFIED
        assert res.markset.spread_s == pytest.approx(1.0)
        assert res.snapshot.status is D.NOT_REQUIRED
        assert res.account_mark_coherence.status is D.NOT_REQUIRED

    def test_spread_exact_limit(self):
        first = _mark(
            position_id="P1",
            tick_time=dt(-4),
            received_at=dt(-3.5),
        )
        second = _mark(
            position_id="P2",
            tick_time=dt(-2),
            received_at=dt(-1.5),
        )
        res = _eval(
            _input(scope=S.MARK_SET, marks=(first, second))
        )
        assert res.markset.spread_s == pytest.approx(2.0)
        assert res.markset.status is D.VERIFIED
        assert res.overall is O.TEMPORALLY_VERIFIED

    def test_spread_exceeded(self):
        first = _mark(
            position_id="P1",
            tick_time=dt(-4),
            received_at=dt(-3.5),
        )
        second = _mark(
            position_id="P2",
            tick_time=dt(-1.9),
            received_at=dt(-1.4),
        )
        res = _eval(
            _input(scope=S.MARK_SET, marks=(first, second))
        )
        assert res.markset.status is D.EXCEEDED
        assert "MARK_SKEW_EXCEEDED" in res.markset.reason_codes
        assert res.overall is O.TEMPORALLY_UNVERIFIED

    def test_empty_mark_set(self):
        res = _eval(_input(scope=S.MARK_SET, marks=()))
        assert res.markset.status is D.MISSING
        assert res.markset.reason_codes == ("MARK_SET_EMPTY",)
        assert res.overall is O.TEMPORALLY_UNVERIFIED

    def test_contradictory_duplicates(self):
        first = _mark(
            position_id="P1",
            tick_time=dt(-2),
            received_at=dt(-1),
        )
        second = _mark(
            position_id="P1",
            tick_time=dt(-1),
            received_at=dt(-0.5),
        )
        res = _eval(
            _input(scope=S.MARK_SET, marks=(first, second))
        )
        assert res.markset.status is D.INVALID
        assert "MARK_SET_INVALID" in res.markset.reason_codes

    def test_consistent_duplicates_are_allowed(self):
        first = _mark(
            position_id="P1",
            tick_time=dt(-1),
            received_at=dt(-0.5),
        )
        second = _mark(
            position_id="P1",
            tick_time=dt(-1),
            received_at=dt(-0.5),
        )
        res = _eval(
            _input(scope=S.MARK_SET, marks=(first, second))
        )
        assert res.markset.status is D.VERIFIED
        assert res.markset.spread_s == pytest.approx(0.0)

    def test_different_domains_not_comparable(self):
        first = _mark(position_id="P1", clock_domain_id="TICK-A")
        second = _mark(
            position_id="P2",
            clock_domain_id="TICK-B",
            tick_time=dt(-1),
            received_at=dt(-0.5),
        )
        res = _eval(
            _input(scope=S.MARK_SET, marks=(first, second))
        )
        assert res.markset.status is D.UNVERIFIED
        assert "MARK_SKEW_UNVERIFIED" in res.markset.reason_codes

    def test_unconfirmed_domain_not_comparable(self):
        first = _mark(position_id="P1")
        second = _mark(
            position_id="P2",
            tick_time=dt(-1),
            received_at=dt(-0.5),
            tick_time_domain_confirmed=False,
        )
        res = _eval(
            _input(scope=S.MARK_SET, marks=(first, second))
        )
        assert res.markset.status is D.UNVERIFIED
        assert "MARK_SKEW_UNVERIFIED" in res.markset.reason_codes

    def test_mark_only_mark_set_not_required(self):
        res = _eval()
        assert res.markset.status is D.NOT_REQUIRED


class TestSnapshot:
    def test_valid_account_and_marks(self):
        res = _eval(_account_input())
        assert res.overall is O.TEMPORALLY_VERIFIED
        assert res.snapshot.status is D.VERIFIED
        assert res.snapshot.age_s == pytest.approx(0.5)
        assert res.account_mark_coherence.status is D.VERIFIED
        assert res.account_mark_coherence.max_abs_skew_s == pytest.approx(
            0.5
        )

    def test_snapshot_missing(self):
        res = _eval(_account_input(snapshot_observed_at=None))
        assert res.snapshot.status is D.MISSING
        assert "SNAPSHOT_TIME_MISSING" in res.snapshot.reason_codes
        assert res.account_mark_coherence.status is D.MISSING
        assert res.overall is O.TEMPORALLY_UNVERIFIED

    def test_snapshot_invalid(self):
        res = _eval(
            _account_input(
                snapshot_observed_at=datetime(2026, 1, 1, 11, 59, 59)
            )
        )
        assert res.snapshot.status is D.INVALID
        assert "SNAPSHOT_TIME_INVALID" in res.snapshot.reason_codes

    def test_snapshot_future(self):
        res = _eval(_account_input(snapshot_observed_at=dt(1)))
        assert res.snapshot.status is D.INVALID
        assert "SNAPSHOT_TIME_FUTURE" in res.snapshot.reason_codes

    def test_snapshot_age_exact_limit_passes(self):
        res = _eval(_account_input(snapshot_observed_at=dt(-5)))
        assert res.snapshot.age_s == pytest.approx(5.0)
        assert res.snapshot.status is D.VERIFIED

    def test_snapshot_too_old(self):
        res = _eval(_account_input(snapshot_observed_at=dt(-6)))
        assert res.snapshot.status is D.EXCEEDED
        assert "SNAPSHOT_AGE_EXCEEDED" in res.snapshot.reason_codes

    def test_snapshot_skew_exact_limit(self):
        res = _eval(
            _account_input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.0),
                marks=(_mark(tick_time=dt(-3), received_at=dt(-2.5)),),
                snapshot_observed_at=dt(-2),
            )
        )
        assert res.account_mark_coherence.status is D.VERIFIED
        assert res.account_mark_coherence.max_abs_skew_s == pytest.approx(
            1.0
        )

    def test_snapshot_skew_exceeded(self):
        res = _eval(
            _account_input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.0),
                marks=(_mark(tick_time=dt(-3), received_at=dt(-2.5)),),
                snapshot_observed_at=dt(-1.9),
            )
        )
        assert res.account_mark_coherence.status is D.EXCEEDED
        assert (
            "SNAPSHOT_SKEW_EXCEEDED"
            in res.account_mark_coherence.reason_codes
        )

    def test_snapshot_skew_interval_crosses_limit(self):
        res = _eval(
            _account_input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.5),
                marks=(_mark(tick_time=dt(-3), received_at=dt(-2.5)),),
                snapshot_observed_at=dt(-2),
            )
        )
        assert res.account_mark_coherence.status is D.UNVERIFIED
        assert (
            "SNAPSHOT_SKEW_UNVERIFIED"
            in res.account_mark_coherence.reason_codes
        )

    def test_snapshot_coherence_requires_offset(self):
        res = _eval(
            _account_input(clock_offset_estimate=None)
        )
        assert (
            res.account_mark_coherence.status is D.UNVERIFIED
        )

    def test_snapshot_read_span_missing(self):
        res = _eval(
            _account_input(
                snapshot_read_started_monotonic=None,
                snapshot_read_finished_monotonic=None,
            )
        )
        assert "SNAPSHOT_READ_SPAN_MISSING" in res.snapshot.reason_codes

    def test_snapshot_read_span_non_monotonic(self):
        res = _eval(
            _account_input(
                snapshot_read_started_monotonic=200.1,
                snapshot_read_finished_monotonic=200.0,
            )
        )
        assert "READ_CLOCK_NON_MONOTONIC" in res.snapshot.reason_codes

    def test_snapshot_read_span_exceeded(self):
        res = _eval(
            _account_input(
                snapshot_read_started_monotonic=200.0,
                snapshot_read_finished_monotonic=201.5,
            )
        )
        assert (
            "SNAPSHOT_READ_SPAN_EXCEEDED"
            in res.snapshot.reason_codes
        )

    def test_snapshot_read_span_exact_limit_is_inclusive(self):
        res = _eval(
            _account_input(
                snapshot_read_started_monotonic=200.0,
                snapshot_read_finished_monotonic=201.0,
            )
        )
        assert res.snapshot.read_span_s == pytest.approx(1.0)
        assert res.snapshot.status is D.VERIFIED
        assert res.overall is O.TEMPORALLY_VERIFIED


class TestReadSpan:
    def test_missing_bounds(self):
        res = _eval(
            _input(
                marks=(
                    _mark(
                        read_started_monotonic=None,
                        read_finished_monotonic=None,
                    ),
                )
            )
        )
        mark = res.markset.marks[0]
        assert "READ_SPAN_MISSING" in mark.reason_codes
        assert mark.status is D.MISSING

    def test_non_monotonic(self):
        res = _eval(
            _input(
                marks=(
                    _mark(
                        read_started_monotonic=100.1,
                        read_finished_monotonic=100.0,
                    ),
                )
            )
        )
        mark = res.markset.marks[0]
        assert "READ_CLOCK_NON_MONOTONIC" in mark.reason_codes
        assert mark.status is D.INVALID

    def test_exact_limit(self):
        res = _eval(
            _input(
                marks=(
                    _mark(
                        read_started_monotonic=100.0,
                        read_finished_monotonic=101.0,
                    ),
                )
            )
        )
        mark = res.markset.marks[0]
        assert mark.read_span_s == pytest.approx(1.0)
        assert mark.status is D.VERIFIED

    def test_exceeded(self):
        res = _eval(
            _input(
                marks=(
                    _mark(
                        read_started_monotonic=100.0,
                        read_finished_monotonic=101.5,
                    ),
                )
            )
        )
        mark = res.markset.marks[0]
        assert "READ_SPAN_EXCEEDED" in mark.reason_codes
        assert mark.status is D.EXCEEDED

    def test_non_numeric_bound(self):
        res = _eval(
            _input(
                marks=(
                    _mark(
                        read_started_monotonic="x",
                        read_finished_monotonic=100.1,
                    ),
                )
            )
        )
        assert "READ_SPAN_MISSING" in res.markset.marks[0].reason_codes


class TestPolicy:
    def test_not_required_field_can_be_none(self):
        res = _eval(_input(), _policy(max_mark_skew_s=None))
        assert res.overall is O.TEMPORALLY_VERIFIED

    def test_missing_required_field_blocks(self):
        res = _eval(_input(), _policy(max_mark_age_s=None))
        assert res.overall is O.TEMPORAL_CHECK_BLOCKED
        assert res.config_status is D.MISSING
        assert res.reason_codes == ("TEMPORAL_CONFIG_MISSING",)

    def test_mark_set_requires_skew(self):
        res = _eval(
            _input(scope=S.MARK_SET, marks=(_mark(),)),
            _policy(max_mark_skew_s=None),
        )
        assert res.overall is O.TEMPORAL_CHECK_BLOCKED
        assert "TEMPORAL_CONFIG_MISSING" in res.reason_codes

    def test_account_requires_snapshot_limits(self):
        res = _eval(
            _account_input(),
            _policy(max_snapshot_age_s=None),
        )
        assert res.overall is O.TEMPORAL_CHECK_BLOCKED
        assert "TEMPORAL_CONFIG_MISSING" in res.reason_codes

    @pytest.mark.parametrize(
        "value",
        [-1.0, True, float("nan"), float("inf"), "5"],
    )
    def test_invalid_threshold(self, value):
        res = _eval(_input(), _policy(max_mark_age_s=value))
        assert res.overall is O.TEMPORAL_CHECK_BLOCKED
        assert res.config_status is D.INVALID
        assert "TEMPORAL_CONFIG_INVALID" in res.reason_codes

    def test_invalid_not_required_field(self):
        res = _eval(
            _input(),
            _policy(max_snapshot_age_s=-1.0),
        )
        assert res.overall is O.TEMPORAL_CHECK_BLOCKED
        assert "TEMPORAL_CONFIG_INVALID" in res.reason_codes

    def test_zero_means_strict_not_unlimited(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.0),
                marks=(_mark(tick_time=NOW, received_at=NOW),),
            ),
            _policy(
                max_mark_age_s=0.0,
                max_receive_delay_s=0.0,
                max_receive_age_s=0.0,
                max_clock_offset_uncertainty_s=0.0,
            ),
        )
        assert res.overall is O.TEMPORALLY_VERIFIED
        assert res.markset.marks[0].status is D.VERIFIED

        res = _eval(
            _input(
                clock_offset_estimate=_offset(uncertainty_seconds=0.0),
                marks=(
                    _mark(
                        tick_time=dt(-0.001),
                        received_at=dt(-0.001),
                    ),
                ),
            ),
            _policy(
                max_mark_age_s=0.0,
                max_receive_delay_s=0.0,
                max_receive_age_s=0.0,
                max_clock_offset_uncertainty_s=0.0,
            ),
        )
        assert "TICK_TOO_OLD" in res.markset.marks[0].reason_codes

    def test_zero_uncertainty_is_strict(self):
        res = _eval(
            _input(
                clock_offset_estimate=_offset(
                    uncertainty_seconds=0.0001
                )
            ),
            _policy(max_clock_offset_uncertainty_s=0.0),
        )
        assert "CLOCK_OFFSET_UNCERTAINTY_EXCEEDED" in res.reason_codes

    def test_zero_read_span_is_strict(self):
        res = _eval(
            _input(),
            _policy(max_read_span_s=0.0),
        )
        assert "READ_SPAN_EXCEEDED" in res.markset.marks[0].reason_codes


class TestScopes:
    def test_mark_only_not_required_dimensions(self):
        res = _eval()
        assert res.markset.status is D.NOT_REQUIRED
        assert res.snapshot.status is D.NOT_REQUIRED
        assert res.account_mark_coherence.status is D.NOT_REQUIRED
        assert len(res.markset.marks) == 1

    def test_mark_only_without_marks_is_not_verified(self):
        # MARK_ONLY no exige la dimension de conjunto (skew), pero sin marcas
        # no hay nada que evaluar: el resultado global no puede verificarse.
        res = _eval(_input(scope=S.MARK_ONLY, marks=()))
        assert res.markset.status is D.NOT_REQUIRED
        assert res.markset.marks == ()
        assert res.overall is O.TEMPORALLY_UNVERIFIED
        assert res.reason_code == "MARK_SET_EMPTY"
        assert res.reason_codes == ("MARK_SET_EMPTY",)

    def test_mark_set_not_required_snapshot(self):
        first = _mark(position_id="P1")
        second = _mark(
            position_id="P2",
            tick_time=dt(-1),
            received_at=dt(-0.5),
        )
        res = _eval(
            _input(scope=S.MARK_SET, marks=(first, second))
        )
        assert res.markset.status is D.VERIFIED
        assert res.snapshot.status is D.NOT_REQUIRED
        assert res.account_mark_coherence.status is D.NOT_REQUIRED

    def test_account_all_dimensions_required(self):
        res = _eval(_account_input())
        assert res.markset.status is D.VERIFIED
        assert res.snapshot.status is D.VERIFIED
        assert res.account_mark_coherence.status is D.VERIFIED

    def test_account_snapshot_issue_prevents_global_verification(self):
        res = _eval(
            _account_input(
                snapshot_observed_at=None,
            )
        )
        assert res.markset.status is D.VERIFIED
        assert res.overall is O.TEMPORALLY_UNVERIFIED


class TestReasonOrdering:
    def test_multiple_reasons_are_canonical_and_deduplicated(self):
        res = _eval(
            _input(
                marks=(
                    _mark(
                        tick_time=dt(1),
                        tick_flags=None,
                        read_started_monotonic=None,
                        read_finished_monotonic=None,
                    ),
                )
            )
        )
        mark = res.markset.marks[0]
        expected = (
            "TICK_TIME_FUTURE",
            "RECEIVE_DELAY_UNVERIFIED",
            "MARK_FLAGS_MISSING",
            "READ_SPAN_MISSING",
        )
        assert mark.reason_codes == expected
        assert mark.reason_code == expected[0]
        assert len(set(mark.reason_codes)) == len(mark.reason_codes)

    def test_global_reasons_deduplicated_across_marks(self):
        first = _mark(position_id="P1", tick_flags=None)
        second = _mark(
            position_id="P2",
            tick_time=dt(-1),
            received_at=dt(-0.5),
            tick_flags=None,
        )
        res = _eval(
            _input(scope=S.MARK_SET, marks=(first, second))
        )
        assert res.reason_codes.count("MARK_FLAGS_MISSING") == 1

    def test_no_reason_for_out_of_scope_dimension(self):
        res = _eval()
        assert res.markset.reason_codes == ()
        assert res.snapshot.reason_codes == ()
        assert res.account_mark_coherence.reason_codes == ()

    def test_reason_code_is_first(self):
        res = _eval(_input(clock_offset_estimate=None))
        assert res.reason_code == res.reason_codes[0]


class TestNoTerminalDependency:
    def test_no_forbidden_imports(self):
        source = inspect.getsource(temporal)
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert not any(
            name.startswith("MetaTrader5")
            or name.startswith("brokers")
            for name in imported
        )
        assert "time" not in imported

    def test_public_results_are_immutable(self):
        res = _eval()
        with pytest.raises(dataclasses.FrozenInstanceError):
            res.overall = O.TEMPORALLY_UNVERIFIED  # type: ignore[misc]

    def test_result_types_exposed(self):
        res = _eval(_account_input())
        assert isinstance(res, MT5TemporalCoherenceResult)
        assert isinstance(res.offset, MT5ClockOffsetValidation)
        assert isinstance(res.markset, MT5MarkSetTemporalResult)
        assert isinstance(res.snapshot, MT5SnapshotTemporalResult)
        assert isinstance(
            res.account_mark_coherence, MT5AccountMarkTemporalResult
        )
        assert isinstance(res.markset.marks[0], MT5MarkTemporalResult)
