"""Autorizacion de ejecucion asimetrica de un solo uso (ORDER-09D).

El coordinador firma con una clave privada Ed25519 una representacion CANONICA,
inmutable y completa de la operacion efectiva. El adapter MT5 recibe SOLO la
clave publica y un verificador que:

  * comprueba la firma Ed25519;
  * exige que la intencion firmada sea EXACTAMENTE la operacion que el adapter
    va a enviar (incluidos lado, cantidad, tipo, reduce_only, posicion objetivo,
    precios limite/stop y cuenta);
  * exige que el `BrokerScope` firmado (entorno, servidor y login MT5) coincida
    con la identidad efectiva observada en `account_info()`;
  * exige que el `session_id` aleatorio de proceso coincida (un reinicio
    invalida autorizaciones anteriores aunque se reutilice la clave privada);
  * consume el nonce de forma atomica con reloj monotono antes de `order_send`.

Separacion de capacidades:

  * `ExecutionAuthority` (emisor) conserva la clave privada y emite.
  * `ExecutionVerifier` conserva unicamente la clave publica, la sesion de
    proceso (id aleatorio + registro de nonces) y un reloj monotono. NO conserva
    referencia al emisor ni puede firmar.

Modelo de amenaza (honesto): la firma asimetrica impide que quien solo posee el
verificador (por ejemplo, el adapter) recupere la capacidad de firma. NO protege
frente a codigo malicioso con acceso al coordinador, a la clave privada o al
proceso Python completo; eso requiere aislamiento de proceso o un firmante
externo, fuera de este bloque.
"""

from __future__ import annotations

import json
import math
import secrets
import threading
import time
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

AUTHORIZATION_VERSION = 2

KIND_OPEN = "OPEN"
KIND_CLOSE = "CLOSE"
KIND_REDUCE = "REDUCE"
KIND_CANCEL = "CANCEL"
_ORDER_KINDS = frozenset({KIND_OPEN, KIND_CLOSE, KIND_REDUCE})
_ALL_KINDS = frozenset(_ORDER_KINDS | {KIND_CANCEL})

_ED25519_SIGNATURE_BYTES = 64


class ExecutionAuthorizationError(Exception):
    """Error base de autorizacion de ejecucion."""


class ExecutionIntentError(ExecutionAuthorizationError):
    """La intencion de ejecucion no es canonica/coherente."""


class ExecutionAuthorizationRequiredError(ExecutionAuthorizationError):
    """No se presento autorizacion para una operacion que la exige."""


class ExecutionAuthorizationInvalidError(ExecutionAuthorizationError):
    """Autorizacion malformada, firma invalida o no ligada a la operacion."""


class ExecutionAuthorizationExpiredError(ExecutionAuthorizationError):
    """Autorizacion fuera de su ventana de validez."""


class ExecutionAuthorizationReusedError(ExecutionAuthorizationError):
    """Nonce ya consumido: replay de una autorizacion de un solo uso."""


class ExecutionAuthorizationSessionError(ExecutionAuthorizationError):
    """Autorizacion emitida por otra sesion de proceso (reinicio)."""


# ---------------------------------------------------------------------------
# Canonicalizacion estricta (sin str(None), sin NaN/Inf, sin ambiguedad)
# ---------------------------------------------------------------------------


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExecutionIntentError(f"{field} debe ser una cadena no vacia")
    return value


def _optional_text(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ExecutionIntentError(f"{field} debe ser cadena o None")
    return value


def _canonical_enum(value: Any, field: str) -> str:
    raw = getattr(value, "value", None)
    if isinstance(raw, str):
        text = raw
    elif isinstance(value, str):
        text = value
    else:
        raise ExecutionIntentError(f"{field} debe ser un enum/cadena")
    if not text:
        raise ExecutionIntentError(f"{field} no puede ser vacio")
    return text


def _canonical_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ExecutionIntentError(f"{field} debe ser booleano")
    return value


def _canonical_login(value: Any) -> str:
    if isinstance(value, bool):
        raise ExecutionIntentError("login no admite booleano")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ExecutionIntentError("login debe ser int o str")
    if not text:
        raise ExecutionIntentError("login no puede ser vacio")
    return text


@dataclass(frozen=True, slots=True)
class BrokerScope:
    """Identidad canonica de la cuenta/terminal que autoriza la operacion.

    `environment` es el entorno efectivo (demo). `server` y `login` identifican
    la cuenta MT5. El scope se firma dentro de la autorizacion y el adapter lo
    compara con `account_info()` justo antes de consumir y enviar.
    """

    environment: str
    server: str
    login: str

    def __post_init__(self) -> None:
        environment = _required_text(self.environment, "environment").strip().lower()
        server = _required_text(self.server, "server").strip()
        login = _canonical_login(self.login)
        if not environment:
            raise ExecutionIntentError("environment no puede ser vacio")
        if not server:
            raise ExecutionIntentError("server no puede ser vacio")
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "server", server)
        object.__setattr__(self, "login", login)

    def as_dict(self) -> dict[str, str]:
        return {
            "environment": self.environment,
            "server": self.server,
            "login": self.login,
        }

    def __repr__(self) -> str:
        masked = "***" + self.login[-4:] if len(self.login) > 4 else "***"
        return (
            f"BrokerScope(environment={self.environment!r}, "
            f"server={self.server!r}, login={masked})"
        )


def _canonical_decimal(
    value: Any, field: str, *, optional: bool = False
) -> str:
    if value is None:
        if optional:
            return ""
        raise ExecutionIntentError(f"{field} es obligatorio")
    if isinstance(value, bool):
        raise ExecutionIntentError(f"{field} no admite booleano")
    if isinstance(value, float) and not math.isfinite(value):
        raise ExecutionIntentError(f"{field} debe ser finito")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExecutionIntentError(f"{field} no es numerico") from exc
    if not number.is_finite():
        raise ExecutionIntentError(f"{field} debe ser finito")
    if number == 0:
        return "0"
    text = format(number.normalize(), "f")
    if text in ("", "-0"):
        return "0"
    return text


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    """Operacion efectiva autorizada, independiente de objetos mutables.

    Todos los campos numericos se guardan como cadenas decimales canonicas.
    `target` es el objetivo semantico del gate: cid para OPEN/CANCEL e
    instrumento para CLOSE/REDUCE. `target_position_id` es la identidad exacta
    de posicion en HEDGING. `target_broker_order_id` liga una cancelacion al
    ticket/orden resuelto.
    """

    version: int
    kind: str
    purpose: str
    op_id: str
    target: str
    client_order_id: str
    instrument: str
    account_id: str
    side: str
    quantity: str
    order_type: str
    time_in_force: str
    reduce_only: bool
    target_position_id: str
    limit_price: str
    stop_price: str
    target_broker_order_id: str = ""

    def __post_init__(self) -> None:
        if self.version != AUTHORIZATION_VERSION:
            raise ExecutionIntentError(
                f"version de intencion no soportada: {self.version!r}"
            )
        if self.kind not in _ALL_KINDS:
            raise ExecutionIntentError(f"kind no soportado: {self.kind!r}")
        for field in (
            "op_id",
            "target",
            "client_order_id",
            "instrument",
            "account_id",
        ):
            if not isinstance(getattr(self, field), str) or not getattr(self, field):
                raise ExecutionIntentError(f"{field} debe ser cadena no vacia")
        if not isinstance(self.target_position_id, str):
            raise ExecutionIntentError("target_position_id debe ser cadena")
        if not isinstance(self.target_broker_order_id, str):
            raise ExecutionIntentError("target_broker_order_id debe ser cadena")
        if not isinstance(self.reduce_only, bool):
            raise ExecutionIntentError("reduce_only debe ser booleano")
        for field in ("limit_price", "stop_price"):
            if not isinstance(getattr(self, field), str):
                raise ExecutionIntentError(f"{field} debe ser cadena")

        if self.kind != KIND_CANCEL:
            if self.side not in ("BUY", "SELL"):
                raise ExecutionIntentError(
                    f"side no soportado: {self.side!r}"
                )
            if self.order_type not in ("MARKET", "LIMIT", "STOP"):
                raise ExecutionIntentError(
                    f"order_type no soportado: {self.order_type!r}"
                )

        if self.kind == KIND_CANCEL:
            if self.purpose != KIND_CANCEL:
                raise ExecutionIntentError("CANCEL exige purpose=CANCEL")
            if self.target != self.client_order_id:
                raise ExecutionIntentError("CANCEL exige target=client_order_id")
            if (
                self.side
                or self.quantity
                or self.order_type
                or self.time_in_force
                or self.target_position_id
                or self.limit_price
                or self.stop_price
            ):
                raise ExecutionIntentError(
                    "CANCEL no admite parametros economicos de orden"
                )
            if self.reduce_only:
                raise ExecutionIntentError("CANCEL no puede ser reduce_only")
            if not self.target_broker_order_id:
                raise ExecutionIntentError(
                    "CANCEL exige target_broker_order_id no vacio"
                )
            return

        if self.purpose != self.kind:
            raise ExecutionIntentError("purpose debe coincidir con kind")
        for field in ("side", "quantity", "order_type", "time_in_force"):
            if not getattr(self, field):
                raise ExecutionIntentError(f"{field} no puede ser vacio")
        if self.target_broker_order_id:
            raise ExecutionIntentError(
                "target_broker_order_id solo aplica a CANCEL"
            )
        if self.kind == KIND_OPEN:
            if self.target != self.client_order_id:
                raise ExecutionIntentError("OPEN exige target=client_order_id")
            if self.reduce_only:
                raise ExecutionIntentError(
                    "purpose OPEN no admite reduce_only=True"
                )
            if self.target_position_id:
                raise ExecutionIntentError(
                    "OPEN no admite target_position_id"
                )
        else:
            if self.target != self.instrument:
                raise ExecutionIntentError(
                    "CLOSE/REDUCE exige target=instrument"
                )
            if not self.reduce_only:
                raise ExecutionIntentError(
                    "CLOSE/REDUCE exige reduce_only=True"
                )

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "kind": self.kind,
            "purpose": self.purpose,
            "op_id": self.op_id,
            "target": self.target,
            "client_order_id": self.client_order_id,
            "instrument": self.instrument,
            "account_id": self.account_id,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "reduce_only": self.reduce_only,
            "target_position_id": self.target_position_id,
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "target_broker_order_id": self.target_broker_order_id,
        }

    @classmethod
    def from_order(cls, order: Any) -> "ExecutionIntent":
        """Construye la intencion canonica desde una `Order` real."""
        if order is None:
            raise ExecutionIntentError("order es obligatorio")
        purpose = _canonical_enum(getattr(order, "purpose", None), "purpose")
        if purpose not in _ORDER_KINDS:
            raise ExecutionIntentError(
                f"purpose de orden no soportado: {purpose!r}"
            )
        reduce_only = _canonical_bool(
            getattr(order, "reduce_only", None), "reduce_only"
        )
        if purpose == KIND_OPEN and reduce_only:
            raise ExecutionIntentError(
                "purpose OPEN no admite reduce_only=True"
            )
        if purpose in (KIND_CLOSE, KIND_REDUCE) and not reduce_only:
            raise ExecutionIntentError(
                f"purpose {purpose} exige reduce_only=True"
            )
        cid = _required_text(
            getattr(order, "client_order_id", None), "client_order_id"
        )
        instrument = _required_text(
            getattr(order, "instrument", None), "instrument"
        )
        account_id = _required_text(
            getattr(order, "account_id", None), "account_id"
        )
        return cls(
            version=AUTHORIZATION_VERSION,
            kind=purpose,
            purpose=purpose,
            op_id=cid,
            target=cid if purpose == KIND_OPEN else instrument,
            client_order_id=cid,
            instrument=instrument,
            account_id=account_id,
            side=_canonical_enum(getattr(order, "side", None), "side"),
            quantity=_canonical_decimal(
                getattr(order, "quantity", None), "quantity"
            ),
            order_type=_canonical_enum(
                getattr(order, "order_type", None), "order_type"
            ),
            time_in_force=_required_text(
                getattr(order, "time_in_force", None), "time_in_force"
            ),
            reduce_only=reduce_only,
            target_position_id=_optional_text(
                getattr(order, "target_position_id", None), "target_position_id"
            ),
            limit_price=_canonical_decimal(
                getattr(order, "limit_price", None), "limit_price", optional=True
            ),
            stop_price=_canonical_decimal(
                getattr(order, "stop_price", None), "stop_price", optional=True
            ),
        )

    @classmethod
    def for_cancel(
        cls,
        *,
        op_id: str,
        target_client_order_id: str,
        instrument: str,
        account_id: str,
        target_broker_order_id: str,
    ) -> "ExecutionIntent":
        target = _required_text(target_client_order_id, "target_client_order_id")
        return cls(
            version=AUTHORIZATION_VERSION,
            kind=KIND_CANCEL,
            purpose=KIND_CANCEL,
            op_id=_required_text(op_id, "op_id"),
            target=target,
            client_order_id=target,
            instrument=_required_text(instrument, "instrument"),
            account_id=_required_text(account_id, "account_id"),
            side="",
            quantity="",
            order_type="",
            time_in_force="",
            reduce_only=False,
            target_position_id="",
            limit_price="",
            stop_price="",
            target_broker_order_id=_required_text(
                target_broker_order_id, "target_broker_order_id"
            ),
        )


@dataclass(frozen=True, slots=True)
class ExecutionAuthorization:
    """Intencion canonica firmada, con nonce y ventana monotona."""

    intent: ExecutionIntent
    broker_scope: BrokerScope
    session_id: str
    nonce: str
    issued_at_mono: float
    expires_at_mono: float
    issued_at_wall: float
    signature: bytes = b""

    def __repr__(self) -> str:
        return (
            "ExecutionAuthorization("
            f"kind={self.intent.kind!r}, op_id={self.intent.op_id!r}, "
            f"client_order_id={self.intent.client_order_id!r}, "
            f"instrument={self.intent.instrument!r}, "
            f"broker_scope={self.broker_scope!r}, "
            f"session_id={self.session_id!r}, "
            f"nonce={self.nonce!r}, "
            f"expires_at_mono={self.expires_at_mono!r})"
        )


def _signed_payload(authorization: ExecutionAuthorization) -> bytes:
    body = {
        "intent": authorization.intent.as_dict(),
        "broker_scope": authorization.broker_scope.as_dict(),
        "session_id": _required_text(authorization.session_id, "session_id"),
        "nonce": _required_text(authorization.nonce, "nonce"),
        "issued_at_mono": _canonical_decimal(
            authorization.issued_at_mono, "issued_at_mono"
        ),
        "expires_at_mono": _canonical_decimal(
            authorization.expires_at_mono, "expires_at_mono"
        ),
        "issued_at_wall": _canonical_decimal(
            authorization.issued_at_wall, "issued_at_wall"
        ),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


class _NonceStore:
    """Registro compartido de nonces consumidos, con reloj monotono."""

    __slots__ = ("_clock", "_lock", "_consumed")

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._consumed: dict[str, float] = {}

    def consume(self, nonce: str, expires_at_mono: float) -> None:
        with self._lock:
            now = self._clock()
            if expires_at_mono <= now:
                raise ExecutionAuthorizationExpiredError(
                    "autorizacion expirada"
                )
            if nonce in self._consumed:
                raise ExecutionAuthorizationReusedError(
                    "autorizacion ya consumida (replay)"
                )
            self._consumed[nonce] = expires_at_mono
            self._prune_locked(now)

    def _prune_locked(self, now: float) -> None:
        expired = [
            nonce
            for nonce, expires_at in self._consumed.items()
            if expires_at <= now
        ]
        for nonce in expired:
            del self._consumed[nonce]

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._consumed)


class _ProcessSession:
    """Sesion de proceso: id aleatorio no inyectable y nonces compartidos."""

    __slots__ = ("__session_id", "__nonces")

    def __init__(self, clock: Callable[[], float]) -> None:
        self.__session_id = secrets.token_hex(16)
        self.__nonces = _NonceStore(clock)

    @property
    def session_id(self) -> str:
        return self.__session_id

    @property
    def nonces(self) -> _NonceStore:
        return self.__nonces


class ExecutionAuthority:
    """Emisor Ed25519: conserva la clave privada y emite autorizaciones."""

    __slots__ = ("__private_key", "_ttl_s", "_clock", "_wall_clock", "_session")

    def __init__(
        self,
        *,
        private_key: Ed25519PrivateKey | None = None,
        ttl_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if private_key is not None and not isinstance(
            private_key, Ed25519PrivateKey
        ):
            raise TypeError("private_key debe ser Ed25519PrivateKey")
        self.__private_key = private_key or Ed25519PrivateKey.generate()
        self._ttl_s = float(ttl_s)
        if not self._ttl_s > 0:
            raise ValueError("ttl_s debe ser positivo")
        self._clock = clock
        self._wall_clock = wall_clock
        # ORDER-09D.1: sesion de proceso NO inyectable ni persistida. Una nueva
        # inicializacion (aunque reutilice la misma clave privada) genera un id
        # distinto y rechaza autorizaciones de la sesion anterior.
        self._session = _ProcessSession(clock)

    def __repr__(self) -> str:
        return "ExecutionAuthority(<clave privada oculta>)"

    @property
    def ttl_s(self) -> float:
        return self._ttl_s

    @property
    def public_key_bytes(self) -> bytes:
        return self.__private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    def verifier(self) -> "ExecutionVerifier":
        """Vista de verificacion: clave publica + sesion de proceso compartida."""
        return ExecutionVerifier(
            self.__private_key.public_key(),
            clock=self._clock,
            session=self._session,
        )

    def issue(
        self,
        intent: ExecutionIntent,
        *,
        broker_scope: BrokerScope,
    ) -> ExecutionAuthorization:
        """Firma una intencion canonica ligada a broker_scope.

        El llamante debe ser el coordinador; no se admite inyectar session_id.
        """
        if not isinstance(intent, ExecutionIntent):
            raise TypeError("issue exige un ExecutionIntent")
        if not isinstance(broker_scope, BrokerScope):
            raise TypeError("issue exige un BrokerScope")
        now = self._clock()
        unsigned = ExecutionAuthorization(
            intent=intent,
            broker_scope=broker_scope,
            session_id=self._session.session_id,
            nonce=secrets.token_hex(16),
            issued_at_mono=now,
            expires_at_mono=now + self._ttl_s,
            issued_at_wall=self._wall_clock(),
        )
        signature = self.__private_key.sign(_signed_payload(unsigned))
        return replace(unsigned, signature=signature)


class ExecutionVerifier:
    """Verificador de solo clave publica. NO conserva ni puede alcanzar al emisor."""

    __slots__ = ("_public_key", "_clock", "_session")

    def __init__(
        self,
        public_key: Ed25519PublicKey,
        *,
        clock: Callable[[], float] = time.monotonic,
        session: _ProcessSession,
    ) -> None:
        if not isinstance(public_key, Ed25519PublicKey):
            raise TypeError("public_key debe ser Ed25519PublicKey")
        if not isinstance(session, _ProcessSession):
            raise TypeError("session debe ser una sesion de proceso")
        self._public_key = public_key
        self._clock = clock
        self._session = session

    def __repr__(self) -> str:
        return (
            "ExecutionVerifier(<clave publica "
            f"{self.public_key_bytes.hex()[:16]}...>)"
        )

    @property
    def public_key_bytes(self) -> bytes:
        return self._public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    @property
    def consumed_count(self) -> int:
        return self._session.nonces.count

    def consume(
        self,
        authorization: ExecutionAuthorization | None,
        *,
        intent: ExecutionIntent,
        broker_scope: BrokerScope,
    ) -> None:
        """Verifica firma/ligadura y consume el nonce de forma atomica."""
        if authorization is None:
            raise ExecutionAuthorizationRequiredError(
                "operacion sin autorizacion de ejecucion"
            )
        if not isinstance(authorization, ExecutionAuthorization):
            raise ExecutionAuthorizationInvalidError(
                "autorizacion de ejecucion de tipo no valido"
            )
        if not isinstance(intent, ExecutionIntent):
            raise ExecutionAuthorizationInvalidError(
                "intencion esperada de tipo no valido"
            )
        if not isinstance(authorization.intent, ExecutionIntent):
            raise ExecutionAuthorizationInvalidError(
                "intencion firmada de tipo no valido"
            )
        if not isinstance(broker_scope, BrokerScope):
            raise ExecutionAuthorizationInvalidError(
                "broker_scope esperado de tipo no valido"
            )
        if not isinstance(authorization.broker_scope, BrokerScope):
            raise ExecutionAuthorizationInvalidError(
                "broker_scope firmado de tipo no valido"
            )
        if not isinstance(authorization.session_id, str) or not authorization.session_id:
            raise ExecutionAuthorizationInvalidError(
                "session_id de autorizacion no valido"
            )
        if not isinstance(authorization.nonce, str) or not authorization.nonce:
            raise ExecutionAuthorizationInvalidError(
                "nonce de autorizacion no valido"
            )
        signature = authorization.signature
        if not isinstance(signature, (bytes, bytearray)):
            raise ExecutionAuthorizationInvalidError(
                "firma de autorizacion de tipo no valido"
            )
        if len(signature) != _ED25519_SIGNATURE_BYTES:
            raise ExecutionAuthorizationInvalidError(
                "firma de autorizacion con longitud no valida"
            )
        try:
            payload = _signed_payload(authorization)
        except Exception as exc:  # noqa: BLE001 - payload malformado
            raise ExecutionAuthorizationInvalidError(
                "payload de autorizacion invalido"
            ) from exc
        try:
            self._public_key.verify(bytes(signature), payload)
        except InvalidSignature as exc:
            raise ExecutionAuthorizationInvalidError(
                "firma de autorizacion invalida"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - verificador defensivo
            raise ExecutionAuthorizationInvalidError(
                "firma de autorizacion invalida"
            ) from exc
        if authorization.intent.version != AUTHORIZATION_VERSION:
            raise ExecutionAuthorizationInvalidError(
                "version de autorizacion no soportada"
            )
        if authorization.session_id != self._session.session_id:
            raise ExecutionAuthorizationSessionError(
                "autorizacion emitida por otra sesion de proceso"
            )
        if authorization.intent != intent:
            raise ExecutionAuthorizationInvalidError(
                "autorizacion no ligada a la operacion efectiva"
            )
        if authorization.broker_scope != broker_scope:
            raise ExecutionAuthorizationInvalidError(
                "autorizacion no ligada a la cuenta/terminal efectiva"
            )
        now = self._clock()
        if authorization.issued_at_mono > now + 1.0:
            raise ExecutionAuthorizationInvalidError(
                "autorizacion emitida en el futuro"
            )
        self._session.nonces.consume(
            authorization.nonce, authorization.expires_at_mono
        )


__all__ = [
    "AUTHORIZATION_VERSION",
    "BrokerScope",
    "ExecutionAuthorization",
    "ExecutionAuthorizationError",
    "ExecutionAuthorizationExpiredError",
    "ExecutionAuthorizationInvalidError",
    "ExecutionAuthorizationRequiredError",
    "ExecutionAuthorizationReusedError",
    "ExecutionAuthorizationSessionError",
    "ExecutionAuthority",
    "ExecutionIntent",
    "ExecutionIntentError",
    "ExecutionVerifier",
    "KIND_CANCEL",
    "KIND_CLOSE",
    "KIND_OPEN",
    "KIND_REDUCE",
]
