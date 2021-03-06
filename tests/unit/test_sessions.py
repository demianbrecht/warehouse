# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time

from unittest import mock

import msgpack
import redis
import pretend
import pytest

from pyramid.config.views import DefaultViewMapper
from pyramid.interfaces import IViewMapperFactory

import warehouse.sessions

from warehouse.sessions import (
    InvalidSession, Session, SessionFactory, uses_session, includeme,
    session_mapper_factory,
)
from warehouse.utils import crypto


def test_uses_session(monkeypatch):
    vary_wrapper = pretend.call_recorder(lambda fn: fn)
    add_vary = pretend.call_recorder(lambda *varies: vary_wrapper)
    monkeypatch.setattr(warehouse.sessions, "add_vary", add_vary)

    @pretend.call_recorder
    def view(context, request):
        pass

    context = pretend.stub()
    request = pretend.stub()

    wrapped = uses_session(view)
    wrapped(context, request)

    assert view.calls == [pretend.call(context, request)]
    assert request._allow_session
    assert add_vary.calls == [pretend.call("Cookie")]
    assert vary_wrapper.calls == [pretend.call(mock.ANY)]


class TestInvalidSession:

    @pytest.mark.parametrize(
        "method",
        [
            # IDict methods
            "__contains__",
            "__delitem__",
            "__getitem__",
            "__iter__",
            "__len__",
            "__setitem__",
            "clear",
            "copy",
            "fromkeys",
            "get",
            "items",
            "keys",
            "pop",
            "popitem",
            "setdefault",
            "update",
            "values",

            # ISession methods
            "invalidate",
            "flash",
            "changed",
            "get_csrf_token",
            "peek_flash",
            "new_csrf_token",
            "pop_flash",

            # Our custom methods.
            "should_save",
            "get_scoped_csrf_token",
            "has_csrf_token",
        ],
    )
    def test_methods_raise(self, method):
        session = InvalidSession()
        with pytest.raises(RuntimeError):
            getattr(session, method)()

    @pytest.mark.parametrize("name", ["created", "new", "sid"])
    def test_propery_raises(self, name):
        session = InvalidSession()
        with pytest.raises(RuntimeError):
            getattr(session, name)


class TestSession:

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (None, {}),
            ({}, {}),
            ({"foo": "bar"}, {"foo": "bar"}),
        ]
    )
    def test_create_new(self, monkeypatch, data, expected):
        monkeypatch.setattr(time, "time", lambda: 100)
        monkeypatch.setattr(crypto, "random_token", lambda: "123456")
        session = Session(data)

        assert session == expected
        assert session.sid == "123456"
        assert session.new
        assert session.created == 100
        assert not session.invalidated

    @pytest.mark.parametrize(
        ("data", "expected", "new"),
        [
            (None, {}, True),
            ({}, {}, True),
            ({"foo": "bar"}, {"foo": "bar"}, True),
            (None, {}, False),
            ({}, {}, False),
            ({"foo": "bar"}, {"foo": "bar"}, False),
        ]
    )
    def test_create_with_session_id(self, monkeypatch, data, expected, new):
        monkeypatch.setattr(time, "time", lambda: 100)
        session = Session(data, "wat", new)

        assert session == expected
        assert session.sid == "wat"
        assert session.new is new
        assert session.created == 100
        assert not session.invalidated

    def test_changed_marks_as_changed(self):
        session = Session()
        assert not session._changed
        session.changed()
        assert session._changed

    def test_invalidate(self, monkeypatch):
        session_ids = iter(["123456", "7890"])
        monkeypatch.setattr(crypto, "random_token", lambda: next(session_ids))
        session = Session({"foo": "bar"}, "original id", False)

        assert session == {"foo": "bar"}
        assert session.sid == "original id"
        assert not session.new
        assert not session.invalidated

        session.invalidate()

        assert session == {}
        assert session.sid == "123456"
        assert session.new
        assert session.invalidated == {"original id"}

        session.invalidate()

        assert session == {}
        assert session.sid == "7890"
        assert session.new
        assert session.invalidated == {"original id", "123456"}

    def test_invalidate_empty(self):
        session = Session({"foo": "bar"})
        session.invalidate()
        assert session == {}
        assert session.invalidated == set()

    def test_should_save(self):
        session = Session()
        assert not session.should_save()
        session.changed()
        assert session.should_save()

    @pytest.mark.parametrize(
        ("data", "method", "args"),
        [
            ({"foo": "bar"}, "__delitem__", ["foo"]),
            ({}, "__setitem__", ["foo", "bar"]),
            ({}, "clear", []),
            ({"foo": "bar"}, "pop", ["foo"]),
            ({"foo": "bar"}, "popitem", []),
            ({}, "setdefault", ["foo", "bar"]),
            ({}, "update", [{"foo": "bar"}]),
        ],
    )
    def test_methods_call_changed(self, data, method, args):
        session = Session(data)
        session.changed = pretend.call_recorder(lambda: None)
        getattr(session, method)(*args)
        assert session.changed.calls == [pretend.call()]

    @pytest.mark.parametrize(
        ("queue", "expected"),
        [
            (None, "_flash_messages"),
            ("foobar", "_flash_messages.foobar"),
        ],
    )
    def test_generate_flash_key(self, queue, expected):
        session = Session()
        assert session._get_flash_queue_key(queue) == expected

    def test_flash_messages(self):
        session = Session()

        assert session.peek_flash() == []
        assert session.peek_flash(queue="foo") == []
        assert session.pop_flash() == []
        assert session.pop_flash(queue="foo") == []

        session.flash("A Flash Message")
        assert session.peek_flash() == ["A Flash Message"]
        assert session.peek_flash(queue="foo") == []

        session.flash("Another Flash Message", queue="foo")
        assert session.peek_flash() == ["A Flash Message"]
        assert session.peek_flash(queue="foo") == ["Another Flash Message"]

        session.flash("A Flash Message")
        assert session.peek_flash() == ["A Flash Message", "A Flash Message"]
        assert session.peek_flash(queue="foo") == ["Another Flash Message"]

        session.flash("A Flash Message", allow_duplicate=True)
        assert session.peek_flash() == [
            "A Flash Message",
            "A Flash Message",
            "A Flash Message",
        ]
        assert session.peek_flash(queue="foo") == ["Another Flash Message"]

        session.flash("A Flash Message", allow_duplicate=False)
        assert session.peek_flash() == [
            "A Flash Message",
            "A Flash Message",
            "A Flash Message",
        ]
        assert session.peek_flash(queue="foo") == ["Another Flash Message"]

        assert session.pop_flash() == [
            "A Flash Message",
            "A Flash Message",
            "A Flash Message",
        ]
        assert session.pop_flash(queue="foo") == ["Another Flash Message"]

        assert session.peek_flash() == []
        assert session.peek_flash(queue="foo") == []
        assert session.pop_flash() == []
        assert session.pop_flash(queue="foo") == []

    def test_csrf_token(self, monkeypatch):
        tokens = iter(["123456", "7890"])
        monkeypatch.setattr(crypto, "random_token", lambda: next(tokens))
        session = Session()

        assert not session.has_csrf_token()
        assert session.new_csrf_token() == "123456"
        assert session.has_csrf_token()
        assert session.get_csrf_token() == "123456"
        assert session.new_csrf_token() == "7890"
        assert session.has_csrf_token()
        assert session.get_csrf_token() == "7890"

    def test_get_csrf_token_empty(self):
        session = Session()
        session.new_csrf_token = pretend.call_recorder(lambda: "123456")

        assert session.get_csrf_token() == "123456"
        assert session.new_csrf_token.calls == [pretend.call()]

    def test_scoped_csrf_token(self):
        session = Session(session_id="my session id")
        session.get_csrf_token = pretend.call_recorder(lambda: "123456")

        assert session.get_scoped_csrf_token("myscope") == (
            "cdcecc5069f543c8fa99c5ebf0fff014e63b7f618ad789e167b5148c4a81b2d0"
            "02c321a48c611ab34ef6d7f539d5196fa80b05f161586ee8d0eee31808cf3b38"
        )
        assert session.get_csrf_token.calls == [pretend.call()]


class TestSessionFactory:

    def test_initialize(self, monkeypatch):
        timestamp_signer_obj = pretend.stub()
        timestamp_signer_create = pretend.call_recorder(
            lambda secret, salt: timestamp_signer_obj
        )
        monkeypatch.setattr(crypto, "TimestampSigner", timestamp_signer_create)

        strict_redis_obj = pretend.stub()
        strict_redis_cls = pretend.stub(
            from_url=pretend.call_recorder(lambda url: strict_redis_obj),
        )
        monkeypatch.setattr(redis, "StrictRedis", strict_redis_cls)

        session_factory = SessionFactory("mysecret", "my url")

        assert session_factory.signer is timestamp_signer_obj
        assert session_factory.redis is strict_redis_obj
        assert timestamp_signer_create.calls == [
            pretend.call("mysecret", salt="session"),
        ]
        assert strict_redis_cls.from_url.calls == [pretend.call("my url")]

    def test_redis_key(self):
        session_factory = SessionFactory(
            "mysecret", "redis://redis://localhost:6379/0",
        )
        assert session_factory._redis_key("my_session_id") == \
            "warehouse/session/data/my_session_id"

    def test_no_current_session(self, pyramid_request):
        session_factory = SessionFactory(
            "mysecret", "redis://redis://localhost:6379/0",
        )
        session_factory._process_response = pretend.stub()
        session = session_factory(pyramid_request)

        assert len(pyramid_request.response_callbacks) == 1
        assert pyramid_request.response_callbacks[0] is \
            session_factory._process_response

        assert isinstance(session, Session)
        assert session._sid is None
        assert session.new

    def test_invalid_session_id(self, pyramid_request):
        pyramid_request.cookies["session_id"] = "invalid!"

        session_factory = SessionFactory(
            "mysecret", "redis://redis://localhost:6379/0",
        )
        session_factory._process_response = pretend.stub()
        session = session_factory(pyramid_request)

        assert len(pyramid_request.response_callbacks) == 1
        assert pyramid_request.response_callbacks[0] is \
            session_factory._process_response

        assert isinstance(session, Session)
        assert session._sid is None
        assert session.new

    def test_valid_session_id_no_data(self, pyramid_request):
        pyramid_request.cookies["session_id"] = "123456"

        session_factory = SessionFactory(
            "mysecret", "redis://redis://localhost:6379/0",
        )
        session_factory.signer.unsign = pretend.call_recorder(
            lambda session_id, max_age: b"123456"
        )
        session_factory.redis = pretend.stub(
            get=pretend.call_recorder(lambda key: None),
        )
        session_factory._process_response = pretend.stub()
        session = session_factory(pyramid_request)

        assert len(pyramid_request.response_callbacks) == 1
        assert pyramid_request.response_callbacks[0] is \
            session_factory._process_response

        assert session_factory.signer.unsign.calls == [
            pretend.call("123456", max_age=12 * 60 * 60),
        ]

        assert session_factory.redis.get.calls == [
            pretend.call("warehouse/session/data/123456"),
        ]

        assert isinstance(session, Session)
        assert session._sid is None
        assert session.new

    def test_valid_session_id_invalid_data(self, pyramid_request):
        pyramid_request.cookies["session_id"] = "123456"

        session_factory = SessionFactory(
            "mysecret", "redis://redis://localhost:6379/0",
        )
        session_factory.signer.unsign = pretend.call_recorder(
            lambda session_id, max_age: b"123456"
        )
        session_factory.redis = pretend.stub(
            get=pretend.call_recorder(lambda key: b"invalid data"),
        )
        session_factory._process_response = pretend.stub()
        session = session_factory(pyramid_request)

        assert len(pyramid_request.response_callbacks) == 1
        assert pyramid_request.response_callbacks[0] is \
            session_factory._process_response

        assert session_factory.signer.unsign.calls == [
            pretend.call("123456", max_age=12 * 60 * 60),
        ]

        assert session_factory.redis.get.calls == [
            pretend.call("warehouse/session/data/123456"),
        ]

        assert isinstance(session, Session)
        assert session._sid is None
        assert session.new

    def test_valid_session_id_valid_data(self, monkeypatch, pyramid_request):
        msgpack_unpackb = pretend.call_recorder(
            lambda bdata, encoding, use_list: {"foo": "bar"}
        )
        monkeypatch.setattr(msgpack, "unpackb", msgpack_unpackb)

        pyramid_request.cookies["session_id"] = "123456"

        session_factory = SessionFactory(
            "mysecret", "redis://redis://localhost:6379/0",
        )
        session_factory.signer.unsign = pretend.call_recorder(
            lambda session_id, max_age: b"123456"
        )
        session_factory.redis = pretend.stub(
            get=pretend.call_recorder(lambda key: b"valid data"),
        )
        session_factory._process_response = pretend.stub()
        session = session_factory(pyramid_request)

        assert len(pyramid_request.response_callbacks) == 1
        assert pyramid_request.response_callbacks[0] is \
            session_factory._process_response

        assert session_factory.signer.unsign.calls == [
            pretend.call("123456", max_age=12 * 60 * 60),
        ]

        assert session_factory.redis.get.calls == [
            pretend.call("warehouse/session/data/123456"),
        ]

        assert msgpack_unpackb.calls == [
            pretend.call(b"valid data", encoding="utf8", use_list=True),
        ]

        assert isinstance(session, Session)
        assert session == {"foo": "bar"}
        assert session.sid == "123456"
        assert not session.new

    def test_no_save_invalid_session(self, pyramid_request):
        session_factory = SessionFactory(
            "mysecret", "redis://redis://localhost:6379/0",
        )
        session_factory.redis = pretend.stub()
        pyramid_request.session = InvalidSession()
        response = pretend.stub()
        session_factory._process_response(pyramid_request, response)

    def test_noop_unused_session(self, pyramid_request):
        session_factory = SessionFactory(
            "mysecret", "redis://redis://localhost:6379/0",
        )
        session_factory.redis = pretend.stub()
        pyramid_request.session.invalidated = set()
        pyramid_request.session.should_save = pretend.call_recorder(
            lambda: False
        )
        response = pretend.stub()
        session_factory._process_response(pyramid_request, response)
        assert pyramid_request.session.should_save.calls == [pretend.call()]

    def test_invalidated_deletes_no_save(self, pyramid_request):
        session_factory = SessionFactory(
            "mysecret", "redis://redis://localhost:6379/0",
        )
        session_factory.redis = pretend.stub(
            delete=pretend.call_recorder(lambda key: None)
        )
        pyramid_request.session.invalidated = ["1", "2"]
        pyramid_request.session.should_save = pretend.call_recorder(
            lambda: False
        )
        response = pretend.stub(
            delete_cookie=pretend.call_recorder(lambda cookie: None),
        )
        session_factory._process_response(pyramid_request, response)

        assert session_factory.redis.delete.calls == [
            pretend.call("warehouse/session/data/1"),
            pretend.call("warehouse/session/data/2"),
        ]
        assert pyramid_request.session.should_save.calls == [
            pretend.call(),
            pretend.call(),
        ]
        assert response.delete_cookie.calls == [pretend.call("session_id")]

    def test_invalidated_deletes_save_non_secure(self, monkeypatch,
                                                 pyramid_request):
        msgpack_packb = pretend.call_recorder(
            lambda data, encoding, use_bin_type: b"msgpack data"
        )
        monkeypatch.setattr(msgpack, "packb", msgpack_packb)

        session_factory = SessionFactory(
            "mysecret", "redis://redis://localhost:6379/0",
        )
        session_factory.redis = pretend.stub(
            delete=pretend.call_recorder(lambda key: None),
            setex=pretend.call_recorder(lambda key, age, data: None),
        )
        session_factory.signer.sign = pretend.call_recorder(
            lambda data: "cookie data"
        )
        pyramid_request.scheme = "http"
        pyramid_request.session.sid = "123456"
        pyramid_request.session.invalidated = ["1", "2"]
        pyramid_request.session.should_save = pretend.call_recorder(
            lambda: True
        )
        response = pretend.stub(
            set_cookie=pretend.call_recorder(
                lambda cookie, data, max_age, httponly, secure: None
            )
        )
        session_factory._process_response(pyramid_request, response)

        assert session_factory.redis.delete.calls == [
            pretend.call("warehouse/session/data/1"),
            pretend.call("warehouse/session/data/2"),
        ]
        assert msgpack_packb.calls == [
            pretend.call(
                pyramid_request.session,
                encoding="utf8",
                use_bin_type=True,
            ),
        ]
        assert session_factory.redis.setex.calls == [
            pretend.call(
                "warehouse/session/data/123456",
                12 * 60 * 60,
                b"msgpack data",
            ),
        ]
        assert pyramid_request.session.should_save.calls == [
            pretend.call(),
            pretend.call(),
        ]
        assert session_factory.signer.sign.calls == [pretend.call(b"123456")]
        assert response.set_cookie.calls == [
            pretend.call(
                "session_id",
                "cookie data",
                max_age=12 * 60 * 60,
                httponly=True,
                secure=False,
            ),
        ]


class TestSessionMapperFactory:

    def test_non_session_view(self, monkeypatch):
        class FakeMapper:
            def __call__(self, view):
                return view

        mapper = session_mapper_factory(FakeMapper)()

        context = pretend.stub()
        request = pretend.stub(session=Session())

        session = request.session

        @pretend.call_recorder
        def view(context, request):
            assert request.session is not session
            assert isinstance(request.session, InvalidSession)

        wrapped = mapper(view)
        wrapped(context, request)

        assert view.calls == [pretend.call(context, request)]
        assert isinstance(request.session, InvalidSession)

    def test_session_view(self, monkeypatch, pyramid_request):
        class FakeMapper:
            def __call__(self, view):
                return view

        mapper = session_mapper_factory(FakeMapper)()

        context = pretend.stub()
        request = pyramid_request
        request._allow_session = True

        session = request.session

        @pretend.call_recorder
        def view(context, request):
            assert request.session is session

        wrapped = mapper(view)
        wrapped(context, request)

        assert view.calls == [pretend.call(context, request)]
        assert request.session is session


@pytest.mark.parametrize("mapper", [None, True])
def test_includeme(monkeypatch, mapper):
    session_factory_obj = pretend.stub()
    session_factory_cls = pretend.call_recorder(
        lambda secret, url: session_factory_obj
    )
    monkeypatch.setattr(
        warehouse.sessions,
        "SessionFactory",
        session_factory_cls,
    )

    if mapper:
        class Mapper:
            pass

        mapper = Mapper

    mapper_cls = pretend.stub()
    session_mapper_factory = pretend.call_recorder(lambda m: mapper_cls)
    monkeypatch.setattr(
        warehouse.sessions, "session_mapper_factory", session_mapper_factory,
    )

    config = pretend.stub(
        commit=pretend.call_recorder(lambda: None),
        set_session_factory=pretend.call_recorder(lambda factory: None),
        registry=pretend.stub(
            queryUtility=pretend.call_recorder(lambda iface: mapper),
            settings={
                "sessions.secret": "my secret",
                "sessions.url": "my url",
            },
        ),
        set_view_mapper=pretend.call_recorder(lambda m: None)
    )

    includeme(config)

    assert config.set_session_factory.calls == [
        pretend.call(session_factory_obj),
    ]
    assert session_factory_cls.calls == [pretend.call("my secret", "my url")]
    assert config.commit.calls == [pretend.call()]
    assert config.registry.queryUtility.calls == [
        pretend.call(IViewMapperFactory),
    ]
    assert session_mapper_factory.calls == [
        pretend.call(mapper if mapper is not None else DefaultViewMapper),
    ]
    assert config.set_view_mapper.calls == [pretend.call(mapper_cls)]
