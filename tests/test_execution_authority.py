"""Tests ORDER-09D/09D.1: autorizacion Ed25519, scope de cuenta y sesion."""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from execution.execution_authority import (
    BrokerScope,
    ExecutionAuthorizationExpiredError,
    ExecutionAuthorizationInvalidError,
    ExecutionAuthorizationRequiredError,
    ExecutionAuthorizationReusedError,
    ExecutionAuthorizationSessionError,
    ExecutionAuthority,
    ExecutionIntent,
    ExecutionIntentError,
    ExecutionVerifier,
)
from execution.order import (
    PURPOSE_CLOSE,
    PURPOSE_OPEN,
    PURPOSE_REDUCE,
    Order,
    OrderSide,
    OrderType,
)

_SCOPE = BrokerScope(environment="demo", server="S-DEMO", login="1001")


class _Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _issue(authority, intent, *, broker_scope=_SCOPE):
    return authority.issue(intent, broker_scope=broker_scope)


def _consume(verifier, authorization, intent, *, broker_scope=_SCOPE):
    verifier.consume(
        authorization, intent=intent, broker_scope=broker_scope
    )


def _order(
    cid: str = "CID-1",
    *,
    account_id: str = "ACC-1",
    instrument: str = "EURUSD",
    side: OrderSide = OrderSide.SELL,
    quantity: float = 0.01,
    order_type: OrderType = OrderType.MARKET,
    purpose: str = PURPOSE_OPEN,
    reduce_only: bool = False,
    target_position_id: str | None = None,
    limit_price: float | None = None,
    stop_price: float | None = None,
) -> Order:
    return Order(
        strategy_id="s",
        strategy_version="1.0.0",
        account_id=account_id,
        instrument=instrument,
        side=side,
        quantity=quantity,
        order_type=order_type,
        client_order_id=cid,
        purpose=purpose,
        reduce_only=reduce_only,
        target_position_id=target_position_id,
        limit_price=limit_price,
        stop_price=stop_price,
    )


def _open_intent(**over) -> ExecutionIntent:
    values = dict(cid="CID-1", side=OrderSide.SELL, quantity=0.01)
    values.update(over)
    return ExecutionIntent.from_order(_order(**values))


def _close_intent(cid: str = "CLOSE:1", position: str = "POS-A") -> ExecutionIntent:
    return ExecutionIntent.from_order(
        _order(
            cid,
            purpose=PURPOSE_CLOSE,
            reduce_only=True,
            target_position_id=position,
            quantity=0.5,
        )
    )


def _cancel_intent(
    *,
    op_id: str = "CANCEL:CID-1",
    target: str = "CID-1",
    instrument: str = "EURUSD",
    account_id: str = "ACC-1",
    broker_order_id: str = "100",
) -> ExecutionIntent:
    return ExecutionIntent.for_cancel(
        op_id=op_id,
        target_client_order_id=target,
        instrument=instrument,
        account_id=account_id,
        target_broker_order_id=broker_order_id,
    )


class TestIssueConsume:
    def test_valid_authorization_is_consumed_once(self):
        authority = ExecutionAuthority(clock=_Clock())
        intent = _open_intent()
        authorization = _issue(authority, intent)
        verifier = authority.verifier()
        _consume(verifier, authorization, intent)
        assert verifier.consumed_count == 1

    def test_missing_authorization_rejected(self):
        authority = ExecutionAuthority(clock=_Clock())
        with pytest.raises(ExecutionAuthorizationRequiredError):
            _consume(authority.verifier(), None, _open_intent())

    def test_verifier_cannot_issue_or_reach_signer(self):
        authority = ExecutionAuthority(clock=_Clock())
        verifier = authority.verifier()
        assert isinstance(verifier, ExecutionVerifier)
        assert not hasattr(verifier, "issue")
        assert not hasattr(verifier, "_ExecutionVerifier__authority")
        assert getattr(verifier, "_ExecutionVerifier__authority", None) is None
        slots = " ".join(verifier.__slots__).lower()
        assert "private" not in slots
        assert "authority" not in slots
        assert verifier.public_key_bytes == authority.public_key_bytes
        with pytest.raises(AttributeError):
            verifier._session.session_id = "forged-session"

    def test_each_issue_has_unique_nonce(self):
        authority = ExecutionAuthority(clock=_Clock())
        assert _issue(authority, _open_intent()).nonce != _issue(
            authority, _open_intent()
        ).nonce

    def test_issue_rejects_non_intent(self):
        authority = ExecutionAuthority(clock=_Clock())
        with pytest.raises(TypeError):
            authority.issue("not-an-intent", broker_scope=_SCOPE)

    def test_issue_rejects_non_scope(self):
        authority = ExecutionAuthority(clock=_Clock())
        with pytest.raises(TypeError):
            authority.issue(_open_intent(), broker_scope="demo|S|1")

    def test_non_positive_ttl_rejected(self):
        with pytest.raises(ValueError):
            ExecutionAuthority(ttl_s=0)


class TestBrokerScopeBinding:
    @pytest.mark.parametrize(
        "scope",
        [
            BrokerScope("demo", "S-DEMO", "2002"),
            BrokerScope("demo", "S-OTHER", "1001"),
            BrokerScope("live", "S-DEMO", "1001"),
        ],
    )
    def test_authorization_bound_to_broker_scope(self, scope):
        authority = ExecutionAuthority(clock=_Clock())
        intent = _open_intent()
        authorization = _issue(authority, intent)
        with pytest.raises(ExecutionAuthorizationInvalidError):
            _consume(
                authority.verifier(), authorization, intent,
                broker_scope=scope,
            )

    def test_scope_canonicalizes_environment_and_login(self):
        scope = BrokerScope(" DEMO ", " S-DEMO ", 1001)
        assert scope.environment == "demo"
        assert scope.server == "S-DEMO"
        assert scope.login == "1001"

    def test_scope_rejects_incomplete_identity(self):
        for values in (
            ("", "S", "1"),
            ("demo", "", "1"),
            ("demo", "S", ""),
        ):
            with pytest.raises(ExecutionIntentError):
                BrokerScope(*values)


class TestEconomicBinding:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("quantity", 1.0),
            ("side", OrderSide.BUY),
            ("instrument", "GBPUSD"),
            ("order_type", OrderType.LIMIT),
            ("account_id", "ACC-2"),
            ("limit_price", 1.25),
            ("stop_price", 0.75),
        ],
    )
    def test_open_authorization_rejects_changed_economic_field(self, field, value):
        authority = ExecutionAuthority(clock=_Clock())
        authorized = _open_intent()
        authorization = _issue(authority, authorized)
        tampered = _open_intent(**{field: value})
        with pytest.raises(ExecutionAuthorizationInvalidError):
            _consume(authority.verifier(), authorization, tampered)

    def test_authorization_for_small_sell_cannot_authorize_large_sell(self):
        authority = ExecutionAuthority(clock=_Clock())
        authorization = _issue(authority, _open_intent(quantity=0.01))
        with pytest.raises(ExecutionAuthorizationInvalidError):
            _consume(
                authority.verifier(), authorization,
                _open_intent(quantity=1.0),
            )

    def test_authorization_for_sell_cannot_authorize_buy(self):
        authority = ExecutionAuthority(clock=_Clock())
        authorization = _issue(
            authority, _open_intent(side=OrderSide.SELL, quantity=0.01)
        )
        with pytest.raises(ExecutionAuthorizationInvalidError):
            _consume(
                authority.verifier(), authorization,
                _open_intent(side=OrderSide.BUY, quantity=0.01),
            )

    def test_close_authorization_bound_to_position(self):
        authority = ExecutionAuthority(clock=_Clock())
        authorization = _issue(authority, _close_intent(position="POS-A"))
        with pytest.raises(ExecutionAuthorizationInvalidError):
            _consume(
                authority.verifier(), authorization,
                _close_intent(position="POS-B"),
            )

    def test_cancel_authorization_bound_to_target_and_ticket(self):
        authority = ExecutionAuthority(clock=_Clock())
        authorization = _issue(authority, _cancel_intent())
        for tampered in (
            _cancel_intent(target="OTHER"),
            _cancel_intent(instrument="GBPUSD"),
            _cancel_intent(account_id="ACC-2"),
            _cancel_intent(broker_order_id="999"),
        ):
            with pytest.raises(ExecutionAuthorizationInvalidError):
                _consume(authority.verifier(), authorization, tampered)

    def test_open_reduce_only_combination_rejected(self):
        with pytest.raises(ExecutionIntentError):
            ExecutionIntent.from_order(
                _order(purpose=PURPOSE_OPEN, reduce_only=True)
            )

    def test_close_without_reduce_only_rejected(self):
        order = _order(purpose=PURPOSE_CLOSE, reduce_only=True)
        object.__setattr__(order, "reduce_only", False)
        with pytest.raises(ExecutionIntentError):
            ExecutionIntent.from_order(order)

    def test_close_and_reduce_are_distinct(self):
        close = ExecutionIntent.from_order(
            _order(purpose=PURPOSE_CLOSE, reduce_only=True, target_position_id="P")
        )
        reduce = ExecutionIntent.from_order(
            _order(
                purpose=PURPOSE_REDUCE,
                reduce_only=True,
                target_position_id="P",
            )
        )
        authority = ExecutionAuthority(clock=_Clock())
        authorization = _issue(authority, close)
        with pytest.raises(ExecutionAuthorizationInvalidError):
            _consume(authority.verifier(), authorization, reduce)


class TestCanonicalization:
    def test_equivalent_numbers_are_canonical(self):
        first = ExecutionIntent.from_order(_order(quantity=1))
        second = ExecutionIntent.from_order(_order(quantity=1.0))
        third = ExecutionIntent.from_order(_order(quantity=1.00))
        assert first.quantity == second.quantity == third.quantity == "1"

    def test_decimal_trailing_zeros_are_canonical(self):
        first = ExecutionIntent.from_order(_order(quantity=0.1))
        second = ExecutionIntent.from_order(_order(quantity=0.10))
        assert first.quantity == second.quantity == "0.1"

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_numbers_rejected(self, value):
        order = _order()
        object.__setattr__(order, "quantity", value)
        with pytest.raises(ExecutionIntentError):
            ExecutionIntent.from_order(order)

    def test_none_is_not_stringified(self):
        order = _order()
        object.__setattr__(order, "account_id", None)
        with pytest.raises(ExecutionIntentError):
            ExecutionIntent.from_order(order)


class TestRejections:
    def test_forged_signature_rejected(self):
        authority = ExecutionAuthority(clock=_Clock())
        other = ExecutionAuthority(clock=_Clock())
        intent = _open_intent()
        forged = _issue(other, intent)
        with pytest.raises(ExecutionAuthorizationInvalidError):
            _consume(authority.verifier(), forged, intent)

    def test_non_authorization_object_rejected(self):
        authority = ExecutionAuthority(clock=_Clock())
        with pytest.raises(ExecutionAuthorizationInvalidError):
            _consume(authority.verifier(), {"kind": "OPEN"}, _open_intent())

    def test_malformed_signature_rejected_with_typed_error(self):
        authority = ExecutionAuthority(clock=_Clock())
        intent = _open_intent()
        authorization = replace(_issue(authority, intent), signature="not-bytes")
        with pytest.raises(ExecutionAuthorizationInvalidError):
            _consume(authority.verifier(), authorization, intent)

    def test_non_finite_expiry_rejected_with_typed_error(self):
        authority = ExecutionAuthority(clock=_Clock())
        intent = _open_intent()
        authorization = replace(
            _issue(authority, intent), expires_at_mono=float("nan")
        )
        with pytest.raises(ExecutionAuthorizationInvalidError):
            _consume(authority.verifier(), authorization, intent)


class TestExpiryAndReplay:
    def test_expired_authorization_rejected(self):
        clock = _Clock()
        authority = ExecutionAuthority(ttl_s=5.0, clock=clock)
        intent = _open_intent()
        authorization = _issue(authority, intent)
        clock.now += 6.0
        with pytest.raises(ExecutionAuthorizationExpiredError):
            _consume(authority.verifier(), authorization, intent)

    def test_authorization_from_future_rejected(self):
        clock = _Clock()
        authority = ExecutionAuthority(ttl_s=60.0, clock=clock)
        intent = _open_intent()
        authorization = _issue(authority, intent)
        clock.now -= 5.0
        with pytest.raises(ExecutionAuthorizationInvalidError):
            _consume(authority.verifier(), authorization, intent)

    def test_replay_rejected(self):
        authority = ExecutionAuthority(clock=_Clock())
        intent = _open_intent()
        authorization = _issue(authority, intent)
        verifier = authority.verifier()
        _consume(verifier, authorization, intent)
        with pytest.raises(ExecutionAuthorizationReusedError):
            _consume(verifier, authorization, intent)

    def test_wall_clock_rollback_does_not_revalidate_expired(self):
        mono = _Clock()
        wall = _Clock(10_000.0)
        authority = ExecutionAuthority(
            ttl_s=5.0, clock=mono, wall_clock=wall
        )
        intent = _open_intent()
        authorization = _issue(authority, intent)
        mono.now += 6.0
        wall.now = 0.0
        with pytest.raises(ExecutionAuthorizationExpiredError):
            _consume(authority.verifier(), authorization, intent)

    def test_wall_clock_rollback_does_not_re_enable_consumed_nonce(self):
        mono = _Clock()
        wall = _Clock(10_000.0)
        authority = ExecutionAuthority(
            ttl_s=30.0, clock=mono, wall_clock=wall
        )
        intent = _open_intent()
        authorization = _issue(authority, intent)
        verifier = authority.verifier()
        _consume(verifier, authorization, intent)
        wall.now = 0.0
        with pytest.raises(ExecutionAuthorizationReusedError):
            _consume(verifier, authorization, intent)

    def test_restart_with_other_key_invalidates_previous_authorization(self):
        intent = _open_intent()
        previous = ExecutionAuthority(clock=_Clock())
        authorization = _issue(previous, intent)
        restarted = ExecutionAuthority(clock=_Clock())
        with pytest.raises(ExecutionAuthorizationInvalidError):
            _consume(restarted.verifier(), authorization, intent)


class TestProcessSession:
    def test_same_private_key_gets_different_process_sessions(self):
        private_key = Ed25519PrivateKey.generate()
        first = ExecutionAuthority(private_key=private_key, clock=_Clock())
        second = ExecutionAuthority(private_key=private_key, clock=_Clock())
        intent = _open_intent()
        auth_a = _issue(first, intent)
        auth_b = _issue(second, intent)
        assert auth_a.session_id != auth_b.session_id
        assert first.public_key_bytes == second.public_key_bytes

    def test_old_session_authorization_rejected_after_restart_with_same_key(self):
        private_key = Ed25519PrivateKey.generate()
        previous = ExecutionAuthority(private_key=private_key, clock=_Clock())
        restarted = ExecutionAuthority(private_key=private_key, clock=_Clock())
        intent = _open_intent()
        authorization = _issue(previous, intent)
        with pytest.raises(ExecutionAuthorizationSessionError):
            _consume(restarted.verifier(), authorization, intent)

    def test_authorization_remains_valid_in_its_original_session(self):
        private_key = Ed25519PrivateKey.generate()
        authority = ExecutionAuthority(private_key=private_key, clock=_Clock())
        intent = _open_intent()
        authorization = _issue(authority, intent)
        _consume(authority.verifier(), authorization, intent)

    def test_consumed_authorization_still_rejected_in_same_session(self):
        private_key = Ed25519PrivateKey.generate()
        authority = ExecutionAuthority(private_key=private_key, clock=_Clock())
        intent = _open_intent()
        authorization = _issue(authority, intent)
        verifier = authority.verifier()
        _consume(verifier, authorization, intent)
        with pytest.raises(ExecutionAuthorizationReusedError):
            _consume(verifier, authorization, intent)

    def test_monotonic_expiration_still_works_with_process_session(self):
        private_key = Ed25519PrivateKey.generate()
        clock = _Clock()
        authority = ExecutionAuthority(
            private_key=private_key, ttl_s=5.0, clock=clock
        )
        intent = _open_intent()
        authorization = _issue(authority, intent)
        clock.now += 6.0
        with pytest.raises(ExecutionAuthorizationExpiredError):
            _consume(authority.verifier(), authorization, intent)


class TestConcurrency:
    def test_concurrent_consume_allows_exactly_one(self):
        authority = ExecutionAuthority(clock=_Clock())
        intent = _open_intent()
        authorization = _issue(authority, intent)
        verifier = authority.verifier()
        barrier = threading.Barrier(2)
        successes: list[bool] = []
        lock = threading.Lock()

        def attempt() -> None:
            barrier.wait()
            try:
                _consume(verifier, authorization, intent)
                ok = True
            except ExecutionAuthorizationReusedError:
                ok = False
            with lock:
                successes.append(ok)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert successes.count(True) == 1
        assert successes.count(False) == 1

    def test_two_verifiers_share_the_same_nonce_store(self):
        authority = ExecutionAuthority(clock=_Clock())
        intent = _open_intent()
        authorization = _issue(authority, intent)
        first = authority.verifier()
        second = authority.verifier()
        _consume(first, authorization, intent)
        with pytest.raises(ExecutionAuthorizationReusedError):
            _consume(second, authorization, intent)
