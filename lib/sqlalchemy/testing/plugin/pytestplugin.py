try:
    # installed by bootstrap.py
    import sqla_plugin_base as plugin_base
except ImportError:
    # assume we're a package, use traditional import
    from . import plugin_base

import argparse
import collections
from functools import update_wrapper
import inspect
import itertools
import operator
import os
import re

import pytest

try:
    import xdist  # noqa

    has_xdist = True
except ImportError:
    has_xdist = False


def pytest_addoption(parser):
    group = parser.getgroup("sqlalchemy")

    def make_option(name, **kw):
        callback_ = kw.pop("callback", None)
        if callback_:

            class CallableAction(argparse.Action):
                def __call__(
                    self, parser, namespace, values, option_string=None
                ):
                    callback_(option_string, values, parser)

            kw["action"] = CallableAction

        zeroarg_callback = kw.pop("zeroarg_callback", None)
        if zeroarg_callback:

            class CallableAction(argparse.Action):
                def __init__(
                    self,
                    option_strings,
                    dest,
                    default=False,
                    required=False,
                    help=None,  # noqa
                ):
                    super(CallableAction, self).__init__(
                        option_strings=option_strings,
                        dest=dest,
                        nargs=0,
                        const=True,
                        default=default,
                        required=required,
                        help=help,
                    )

                def __call__(
                    self, parser, namespace, values, option_string=None
                ):
                    zeroarg_callback(option_string, values, parser)

            kw["action"] = CallableAction

        group.addoption(name, **kw)

    plugin_base.setup_options(make_option)
    plugin_base.read_config()


def pytest_configure(config):
    if hasattr(config, "workerinput"):
        plugin_base.restore_important_follower_config(config.workerinput)
        plugin_base.configure_follower(config.workerinput["follower_ident"])
    else:
        if config.option.write_idents and os.path.exists(
            config.option.write_idents
        ):
            os.remove(config.option.write_idents)

    plugin_base.pre_begin(config.option)

    plugin_base.set_coverage_flag(
        bool(getattr(config.option, "cov_source", False))
    )

    plugin_base.set_fixture_functions(PytestFixtureFunctions)

    if config.option.dump_pyannotate:
        global DUMP_PYANNOTATE
        DUMP_PYANNOTATE = True


DUMP_PYANNOTATE = False


@pytest.fixture(autouse=True)
def collect_types_fixture():
    if DUMP_PYANNOTATE:
        from pyannotate_runtime import collect_types

        collect_types.start()
    yield
    if DUMP_PYANNOTATE:
        collect_types.stop()


def pytest_sessionstart(session):
    from sqlalchemy.testing import asyncio

    asyncio._assume_async(plugin_base.post_begin)


def pytest_sessionfinish(session):
    from sqlalchemy.testing import asyncio

    asyncio._maybe_async_provisioning(plugin_base.final_process_cleanup)

    if session.config.option.dump_pyannotate:
        from pyannotate_runtime import collect_types

        collect_types.dump_stats(session.config.option.dump_pyannotate)


def pytest_collection_finish(session):
    if session.config.option.dump_pyannotate:
        from pyannotate_runtime import collect_types

        lib_sqlalchemy = os.path.abspath("lib/sqlalchemy")

        def _filter(filename):
            filename = os.path.normpath(os.path.abspath(filename))
            if "lib/sqlalchemy" not in os.path.commonpath(
                [filename, lib_sqlalchemy]
            ):
                return None
            if "testing" in filename:
                return None

            return filename

        collect_types.init_types_collection(filter_filename=_filter)


if has_xdist:
    import uuid

    def pytest_configure_node(node):
        from sqlalchemy.testing import provision
        from sqlalchemy.testing import asyncio

        # the master for each node fills workerinput dictionary
        # which pytest-xdist will transfer to the subprocess

        plugin_base.memoize_important_follower_config(node.workerinput)

        node.workerinput["follower_ident"] = "test_%s" % uuid.uuid4().hex[0:12]

        asyncio._maybe_async_provisioning(
            provision.create_follower_db, node.workerinput["follower_ident"]
        )

    def pytest_testnodedown(node, error):
        from sqlalchemy.testing import provision
        from sqlalchemy.testing import asyncio

        asyncio._maybe_async_provisioning(
            provision.drop_follower_db, node.workerinput["follower_ident"]
        )


def pytest_collection_modifyitems(session, config, items):

    # look for all those classes that specify __backend__ and
    # expand them out into per-database test cases.

    # this is much easier to do within pytest_pycollect_makeitem, however
    # pytest is iterating through cls.__dict__ as makeitem is
    # called which causes a "dictionary changed size" error on py3k.
    # I'd submit a pullreq for them to turn it into a list first, but
    # it's to suit the rather odd use case here which is that we are adding
    # new classes to a module on the fly.

    from sqlalchemy.testing import asyncio

    rebuilt_items = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )

    items[:] = [
        item
        for item in items
        if item.getparent(pytest.Class) is not None
        and not item.getparent(pytest.Class).name.startswith("_")
    ]

    test_classes = set(item.getparent(pytest.Class) for item in items)

    def collect(element):
        for inst_or_fn in element.collect():
            if isinstance(inst_or_fn, pytest.Collector):
                yield from collect(inst_or_fn)
            else:
                yield inst_or_fn

    def setup_test_classes():
        for test_class in test_classes:
            for sub_cls in plugin_base.generate_sub_tests(
                test_class.cls, test_class.module
            ):
                if sub_cls is not test_class.cls:
                    per_cls_dict = rebuilt_items[test_class.cls]

                    module = test_class.getparent(pytest.Module)
                    for fn in collect(
                        pytest.Class.from_parent(
                            name=sub_cls.__name__, parent=module
                        )
                    ):
                        per_cls_dict[fn.name].append(fn)

    # class requirements will sometimes need to access the DB to check
    # capabilities, so need to do this for async
    asyncio._maybe_async_provisioning(setup_test_classes)

    newitems = []
    for item in items:
        cls_ = item.cls
        if cls_ in rebuilt_items:
            newitems.extend(rebuilt_items[cls_][item.name])
        else:
            newitems.append(item)

    # seems like the functions attached to a test class aren't sorted already?
    # is that true and why's that? (when using unittest, they're sorted)
    items[:] = sorted(
        newitems,
        key=lambda item: (
            item.getparent(pytest.Module).name,
            item.getparent(pytest.Class).name,
            item.name,
        ),
    )


def pytest_pycollect_makeitem(collector, name, obj):
    if inspect.isclass(obj) and plugin_base.want_class(name, obj):
        from sqlalchemy.testing import config

        if config.any_async:
            obj = _apply_maybe_async(obj)

        return [
            pytest.Class.from_parent(
                name=parametrize_cls.__name__, parent=collector
            )
            for parametrize_cls in _parametrize_cls(collector.module, obj)
        ]
    elif (
        inspect.isfunction(obj)
        and collector.cls is not None
        and plugin_base.want_method(collector.cls, obj)
    ):
        # None means, fall back to default logic, which includes
        # method-level parametrize
        return None
    else:
        # empty list means skip this item
        return []


def _is_wrapped_coroutine_function(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__

    return inspect.iscoroutinefunction(fn)


def _apply_maybe_async(obj, recurse=True):
    from sqlalchemy.testing import asyncio

    for name, value in vars(obj).items():
        if (
            (callable(value) or isinstance(value, classmethod))
            and not getattr(value, "_maybe_async_applied", False)
            and (name.startswith("test_"))
            and not _is_wrapped_coroutine_function(value)
        ):
            is_classmethod = False
            if isinstance(value, classmethod):
                value = value.__func__
                is_classmethod = True

            @_pytest_fn_decorator
            def make_async(fn, *args, **kwargs):
                return asyncio._maybe_async(fn, *args, **kwargs)

            do_async = make_async(value)
            if is_classmethod:
                do_async = classmethod(do_async)
            do_async._maybe_async_applied = True

            setattr(obj, name, do_async)
    if recurse:
        for cls in obj.mro()[1:]:
            if cls != object:
                _apply_maybe_async(cls, False)
    return obj


def _parametrize_cls(module, cls):
    """implement a class-based version of pytest parametrize."""

    if "_sa_parametrize" not in cls.__dict__:
        return [cls]

    _sa_parametrize = cls._sa_parametrize
    classes = []
    for full_param_set in itertools.product(
        *[params for argname, params in _sa_parametrize]
    ):
        cls_variables = {}

        for argname, param in zip(
            [_sa_param[0] for _sa_param in _sa_parametrize], full_param_set
        ):
            if not argname:
                raise TypeError("need argnames for class-based combinations")
            argname_split = re.split(r",\s*", argname)
            for arg, val in zip(argname_split, param.values):
                cls_variables[arg] = val
        parametrized_name = "_".join(
            re.sub(r"\W", "", token)
            for param in full_param_set
            for token in param.id.split("-")
        )
        name = "%s_%s" % (cls.__name__, parametrized_name)
        newcls = type.__new__(type, name, (cls,), cls_variables)
        setattr(module, name, newcls)
        classes.append(newcls)
    return classes


_current_class = None


def pytest_runtest_setup(item):
    from sqlalchemy.testing import asyncio

    # pytest_runtest_setup runs *before* pytest fixtures with scope="class".
    # plugin_base.start_test_class_outside_fixtures may opt to raise SkipTest
    # for the whole class and has to run things that are across all current
    # databases, so we run this outside of the pytest fixture system altogether
    # and ensure asyncio greenlet if any engines are async

    global _current_class

    if isinstance(item, pytest.Function) and _current_class is None:
        asyncio._maybe_async_provisioning(
            plugin_base.start_test_class_outside_fixtures,
            item.cls,
        )
        _current_class = item.getparent(pytest.Class)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    # runs inside of pytest function fixture scope
    # after test function runs

    from sqlalchemy.testing import asyncio

    asyncio._maybe_async(plugin_base.after_test, item)

    yield
    # this is now after all the fixture teardown have run, the class can be
    # finalized. Since pytest v7 this finalizer can no longer be added in
    # pytest_runtest_setup since the class has not yet been setup at that
    # time.
    # See https://github.com/pytest-dev/pytest/issues/9343
    global _current_class, _current_report

    if _current_class is not None and (
        # last test or a new class
        nextitem is None
        or nextitem.getparent(pytest.Class) is not _current_class
    ):
        _current_class = None

        try:
            asyncio._maybe_async_provisioning(
                plugin_base.stop_test_class_outside_fixtures, item.cls
            )
        except Exception as e:
            # in case of an exception during teardown attach the original
            # error to the exception message, otherwise it will get lost
            if _current_report.failed:
                if not e.args:
                    e.args = (
                        "__Original test failure__:\n"
                        + _current_report.longreprtext,
                    )
                elif e.args[-1] and isinstance(e.args[-1], str):
                    args = list(e.args)
                    args[-1] += (
                        "\n__Original test failure__:\n"
                        + _current_report.longreprtext
                    )
                    e.args = tuple(args)
                else:
                    e.args += (
                        "__Original test failure__",
                        _current_report.longreprtext,
                    )
            raise
        finally:
            _current_report = None


def pytest_runtest_call(item):
    # runs inside of pytest function fixture scope
    # before test function runs

    from sqlalchemy.testing import asyncio

    asyncio._maybe_async(
        plugin_base.before_test,
        item,
        item.module.__name__,
        item.cls,
        item.name,
    )


_current_report = None


def pytest_runtest_logreport(report):
    global _current_report
    if report.when == "call":
        _current_report = report


@pytest.fixture(scope="class")
def setup_class_methods(request):
    from sqlalchemy.testing import asyncio

    cls = request.cls

    if hasattr(cls, "setup_test_class"):
        asyncio._maybe_async(cls.setup_test_class)

    yield

    if hasattr(cls, "teardown_test_class"):
        asyncio._maybe_async(cls.teardown_test_class)

    asyncio._maybe_async(plugin_base.stop_test_class, cls)


@pytest.fixture(scope="function")
def setup_test_methods(request):
    from sqlalchemy.testing import asyncio

    # called for each test

    self = request.instance

    # before this fixture runs:

    # 1. function level "autouse" fixtures under py3k (examples: TablesTest
    #    define tables / data, MappedTest define tables / mappers / data)

    # 2. was for p2k. no longer applies

    # 3. run outer xdist-style setup
    if hasattr(self, "setup_test"):
        asyncio._maybe_async(self.setup_test)

    # alembic test suite is using setUp and tearDown
    # xdist methods; support these in the test suite
    # for the near term
    if hasattr(self, "setUp"):
        asyncio._maybe_async(self.setUp)

    # inside the yield:
    # 4. function level fixtures defined on test functions themselves,
    #    e.g. "connection", "metadata" run next

    # 5. pytest hook pytest_runtest_call then runs

    # 6. test itself runs

    yield

    # yield finishes:

    # 7. function level fixtures defined on test functions
    #    themselves, e.g. "connection" rolls back the transaction, "metadata"
    #    emits drop all

    # 8. pytest hook pytest_runtest_teardown hook runs, this is associated
    #    with fixtures close all sessions, provisioning.stop_test_class(),
    #    engines.testing_reaper -> ensure all connection pool connections
    #    are returned, engines created by testing_engine that aren't the
    #    config engine are disposed

    asyncio._maybe_async(plugin_base.after_test_fixtures, self)

    # 10. run xdist-style teardown
    if hasattr(self, "tearDown"):
        asyncio._maybe_async(self.tearDown)

    if hasattr(self, "teardown_test"):
        asyncio._maybe_async(self.teardown_test)

    # 11. was for p2k. no longer applies

    # 12. function level "autouse" fixtures under py3k (examples: TablesTest /
    #    MappedTest delete table data, possibly drop tables and clear mappers
    #    depending on the flags defined by the test class)


def _pytest_fn_decorator(target):
    """Port of langhelpers.decorator with pytest-specific tricks."""

    from sqlalchemy.util.langhelpers import format_argspec_plus
    from sqlalchemy.util.compat import inspect_getfullargspec

    def _exec_code_in_env(code, env, fn_name):
        exec(code, env)
        return env[fn_name]

    def decorate(fn, add_positional_parameters=()):

        spec = inspect_getfullargspec(fn)
        if add_positional_parameters:
            spec.args.extend(add_positional_parameters)

        metadata = dict(
            __target_fn="__target_fn", __orig_fn="__orig_fn", name=fn.__name__
        )
        metadata.update(format_argspec_plus(spec, grouped=False))
        code = (
            """\
def %(name)s%(grouped_args)s:
    return %(__target_fn)s(%(__orig_fn)s, %(apply_kw)s)
"""
            % metadata
        )
        decorated = _exec_code_in_env(
            code, {"__target_fn": target, "__orig_fn": fn}, fn.__name__
        )
        if not add_positional_parameters:
            decorated.__defaults__ = getattr(fn, "__func__", fn).__defaults__
            decorated.__wrapped__ = fn
            return update_wrapper(decorated, fn)
        else:
            # this is the pytest hacky part.  don't do a full update wrapper
            # because pytest is really being sneaky about finding the args
            # for the wrapped function
            decorated.__module__ = fn.__module__
            decorated.__name__ = fn.__name__
            if hasattr(fn, "pytestmark"):
                decorated.pytestmark = fn.pytestmark
            return decorated

    return decorate


class PytestFixtureFunctions(plugin_base.FixtureFunctions):
    def skip_test_exception(self, *arg, **kw):
        return pytest.skip.Exception(*arg, **kw)

    def mark_base_test_class(self):
        return pytest.mark.usefixtures(
            "setup_class_methods", "setup_test_methods"
        )

    _combination_id_fns = {
        "i": lambda obj: obj,
        "r": repr,
        "s": str,
        "n": lambda obj: obj.__name__
        if hasattr(obj, "__name__")
        else type(obj).__name__,
    }

    def combinations(self, *arg_sets, **kw):
        """Facade for pytest.mark.parametrize.

        Automatically derives argument names from the callable which in our
        case is always a method on a class with positional arguments.

        ids for parameter sets are derived using an optional template.

        """
        from sqlalchemy.testing import exclusions

        if len(arg_sets) == 1 and hasattr(arg_sets[0], "__next__"):
            arg_sets = list(arg_sets[0])

        argnames = kw.pop("argnames", None)

        def _filter_exclusions(args):
            result = []
            gathered_exclusions = []
            for a in args:
                if isinstance(a, exclusions.compound):
                    gathered_exclusions.append(a)
                else:
                    result.append(a)

            return result, gathered_exclusions

        id_ = kw.pop("id_", None)

        tobuild_pytest_params = []
        has_exclusions = False
        if id_:
            _combination_id_fns = self._combination_id_fns

            # because itemgetter is not consistent for one argument vs.
            # multiple, make it multiple in all cases and use a slice
            # to omit the first argument
            _arg_getter = operator.itemgetter(
                0,
                *[
                    idx
                    for idx, char in enumerate(id_)
                    if char in ("n", "r", "s", "a")
                ],
            )
            fns = [
                (operator.itemgetter(idx), _combination_id_fns[char])
                for idx, char in enumerate(id_)
                if char in _combination_id_fns
            ]

            for arg in arg_sets:
                if not isinstance(arg, tuple):
                    arg = (arg,)

                fn_params, param_exclusions = _filter_exclusions(arg)

                parameters = _arg_getter(fn_params)[1:]

                if param_exclusions:
                    has_exclusions = True

                tobuild_pytest_params.append(
                    (
                        parameters,
                        param_exclusions,
                        "-".join(
                            comb_fn(getter(arg)) for getter, comb_fn in fns
                        ),
                    )
                )

        else:

            for arg in arg_sets:
                if not isinstance(arg, tuple):
                    arg = (arg,)

                fn_params, param_exclusions = _filter_exclusions(arg)

                if param_exclusions:
                    has_exclusions = True

                tobuild_pytest_params.append(
                    (fn_params, param_exclusions, None)
                )

        pytest_params = []
        for parameters, param_exclusions, id_ in tobuild_pytest_params:
            if has_exclusions:
                parameters += (param_exclusions,)

            param = pytest.param(*parameters, id=id_)
            pytest_params.append(param)

        def decorate(fn):
            if inspect.isclass(fn):
                if has_exclusions:
                    raise NotImplementedError(
                        "exclusions not supported for class level combinations"
                    )
                if "_sa_parametrize" not in fn.__dict__:
                    fn._sa_parametrize = []
                fn._sa_parametrize.append((argnames, pytest_params))
                return fn
            else:
                if argnames is None:
                    _argnames = inspect.getfullargspec(fn).args[1:]
                else:
                    _argnames = re.split(r", *", argnames)

                if has_exclusions:
                    _argnames += ["_exclusions"]

                    @_pytest_fn_decorator
                    def check_exclusions(fn, *args, **kw):
                        _exclusions = args[-1]
                        if _exclusions:
                            exlu = exclusions.compound().add(*_exclusions)
                            fn = exlu(fn)
                        return fn(*args[0:-1], **kw)

                    def process_metadata(spec):
                        spec.args.append("_exclusions")

                    fn = check_exclusions(
                        fn, add_positional_parameters=("_exclusions",)
                    )

                return pytest.mark.parametrize(_argnames, pytest_params)(fn)

        return decorate

    def param_ident(self, *parameters):
        ident = parameters[0]
        return pytest.param(*parameters[1:], id=ident)

    def fixture(self, *arg, **kw):
        from sqlalchemy.testing import config
        from sqlalchemy.testing import asyncio

        # wrapping pytest.fixture function.  determine if
        # decorator was called as @fixture or @fixture().
        if len(arg) > 0 and callable(arg[0]):
            # was called as @fixture(), we have the function to wrap.
            fn = arg[0]
            arg = arg[1:]
        else:
            # was called as @fixture, don't have the function yet.
            fn = None

        # create a pytest.fixture marker.  because the fn is not being
        # passed, this is always a pytest.FixtureFunctionMarker()
        # object (or whatever pytest is calling it when you read this)
        # that is waiting for a function.
        fixture = pytest.fixture(*arg, **kw)

        # now apply wrappers to the function, including fixture itself

        def wrap(fn):
            if config.any_async:
                fn = asyncio._maybe_async_wrapper(fn)
            # other wrappers may be added here

            # now apply FixtureFunctionMarker
            fn = fixture(fn)

            return fn

        if fn:
            return wrap(fn)
        else:
            return wrap

    def get_current_test_name(self):
        return os.environ.get("PYTEST_CURRENT_TEST")

    def async_test(self, fn):
        from sqlalchemy.testing import asyncio

        @_pytest_fn_decorator
        def decorate(fn, *args, **kwargs):
            asyncio._run_coroutine_function(fn, *args, **kwargs)

        return decorate(fn)
