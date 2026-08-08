from app.adapters.fake import FakeFunPayAdapter, FakeFunPayTransport, FakeSecureStore
from app.adapters.session_backed_funpay import SessionBackedFunPayAdapter
from app.domain.funpay import FunPayHealth
from app.persistence.database import Database


def test_valid_and_missing_sessions_determine_health():
    store = FakeSecureStore()
    transport = FakeFunPayTransport(FakeFunPayAdapter(), valid_sessions={"valid"})
    adapter = SessionBackedFunPayAdapter("owner", store, transport)
    assert adapter.health() == FunPayHealth.AUTH_REQUIRED
    store.set_funpay_session("owner", "valid")
    assert adapter.health() == FunPayHealth.READY


def test_invalid_session_is_cleared_and_security_challenge_is_fail_closed():
    store = FakeSecureStore()
    transport = FakeFunPayTransport(FakeFunPayAdapter(), valid_sessions={"valid"})
    adapter = SessionBackedFunPayAdapter("owner", store, transport)
    store.set_funpay_session("owner", "invalid")
    assert adapter.health() == FunPayHealth.AUTH_REQUIRED
    assert store.get_funpay_session("owner") is None
    store.set_funpay_session("owner", "valid")
    transport.set_challenge(True)
    assert adapter.health() == FunPayHealth.AUTH_REQUIRED
    assert store.get_funpay_session("owner") is None


def test_unavailable_transport_and_external_action_gating():
    store = FakeSecureStore()
    fake = FakeFunPayAdapter()
    transport = FakeFunPayTransport(fake, valid_sessions={"valid"})
    adapter = SessionBackedFunPayAdapter("owner", store, transport)
    assert not adapter.disable_lots("account", ["lot"]).verified
    assert fake.lot_operations == []
    store.set_funpay_session("owner", "valid")
    transport.set_health(FunPayHealth.UNAVAILABLE)
    assert adapter.health() == FunPayHealth.UNAVAILABLE
    assert not adapter.enable_lots("account", ["lot"]).verified
    assert fake.lot_operations == []


def test_session_value_never_enters_sqlite_or_adapter_logs(tmp_path):
    secret_session = "SESSION_SHOULD_NOT_PERSIST"
    store = FakeSecureStore()
    store.set_funpay_session("owner", secret_session)
    database = Database(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    database.create_schema()
    values = []
    with database.engine.connect() as connection:
        for table in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars():
            values.extend(str(value) for row in connection.exec_driver_sql(f"SELECT * FROM {table}") for value in row)
    assert secret_session not in values
    assert secret_session not in repr(SessionBackedFunPayAdapter("owner", store, FakeFunPayTransport(FakeFunPayAdapter(), valid_sessions={secret_session})))
