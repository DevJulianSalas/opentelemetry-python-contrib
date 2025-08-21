# Copyright The OpenTelemetry Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
The integration with PostgreSQL supports the `Psycopg`_ library, it can be enabled by
using ``Psycopg2Instrumentor``.

.. _Psycopg: http://initd.org/psycopg/

SQLCOMMENTER
*****************************************
You can optionally configure Psycopg2 instrumentation to enable sqlcommenter which enriches
the query with contextual information.

Usage
-----

.. code:: python

    from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor

    Psycopg2Instrumentor().instrument(enable_commenter=True, commenter_options={})


For example,
::

   Invoking cursor.execute("select * from auth_users") will lead to sql query "select * from auth_users" but when SQLCommenter is enabled
   the query will get appended with some configurable tags like "select * from auth_users /*tag=value*/;"


SQLCommenter Configurations
***************************
We can configure the tags to be appended to the sqlquery log by adding configuration inside commenter_options(default:{}) keyword

db_driver = True(Default) or False

For example,
::
Enabling this flag will add psycopg2 and it's version which is /*psycopg2%%3A2.9.3*/

dbapi_threadsafety = True(Default) or False

For example,
::
Enabling this flag will add threadsafety /*dbapi_threadsafety=2*/

dbapi_level = True(Default) or False

For example,
::
Enabling this flag will add dbapi_level /*dbapi_level='2.0'*/

libpq_version = True(Default) or False

For example,
::
Enabling this flag will add libpq_version /*libpq_version=140001*/

driver_paramstyle = True(Default) or False

For example,
::
Enabling this flag will add driver_paramstyle /*driver_paramstyle='pyformat'*/

opentelemetry_values = True(Default) or False

For example,
::
Enabling this flag will add traceparent values /*traceparent='00-03afa25236b8cd948fa853d67038ac79-405ff022e8247c46-01'*/

SQLComment in span attribute
****************************
If sqlcommenter is enabled, you can optionally configure psycopg2 instrumentation to append sqlcomment to query span attribute for convenience of your platform.

.. code:: python

    from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor

    Psycopg2Instrumentor().instrument(
        enable_commenter=True,
        enable_attribute_commenter=True,
    )


For example,
::

    Invoking cursor.execute("select * from auth_users") will lead to postgresql query "select * from auth_users" but when SQLCommenter and attribute_commenter are enabled
    the query will get appended with some configurable tags like "select * from auth_users /*tag=value*/;" for both server query and `db.statement` span attribute.

Usage
-----

.. code-block:: python

    import psycopg2
    from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor

    # Call instrument() to wrap all database connections
    Psycopg2Instrumentor().instrument()

    cnx = psycopg2.connect(database='Database')

    cursor = cnx.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS test (testField INTEGER)")
    cursor.execute("INSERT INTO test (testField) VALUES (123)")
    cursor.close()
    cnx.close()

.. code-block:: python

    import psycopg2
    from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor

    # Alternatively, use instrument_connection for an individual connection
    cnx = psycopg2.connect(database='Database')
    instrumented_cnx = Psycopg2Instrumentor().instrument_connection(cnx)
    cursor = instrumented_cnx.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS test (testField INTEGER)")
    cursor.execute("INSERT INTO test (testField) VALUES (123)")
    cursor.close()
    instrumented_cnx.close()

API
---
"""

import logging
import typing
import copy
from importlib.metadata import PackageNotFoundError, distribution
from typing import Collection

import psycopg2
from psycopg2.extensions import (
    cursor as pg_cursor,  # pylint: disable=no-name-in-module
)
from psycopg2.sql import Composed  # pylint: disable=no-name-in-module
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.instrumentation import dbapi
from opentelemetry.metrics import get_meter
from opentelemetry.instrumentation._semconv import (
    _get_schema_url,
    _OpenTelemetrySemanticConventionStability,
    _OpenTelemetryStabilitySignalType,
    HTTP_DURATION_HISTOGRAM_BUCKETS_NEW
)
from opentelemetry.semconv.metrics.db_metrics import DB_CLIENT_OPERATION_DURATION
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.psycopg2.package import (
    _instruments_any,
    _instruments_psycopg2,
    _instruments_psycopg2_binary,
)
from opentelemetry.instrumentation.psycopg2.version import __version__
from timeit import default_timer


_logger = logging.getLogger(__name__)
_OTEL_CURSOR_FACTORY_KEY = "_otel_orig_cursor_factory"
CursorT = typing.TypeVar("CursorT")


class Psycopg2Instrumentor(BaseInstrumentor):
    _CONNECTION_ATTRIBUTES = {
        "database": "info.dbname",
        "port": "info.port",
        "host": "info.host",
        "user": "info.user",
    }

    _DATABASE_SYSTEM = "postgresql"
    _duration_histogram_db_operation = None
    _error_execution_statements_count = None
    _execution_statements_count = None

    def instrumentation_dependencies(self) -> Collection[str]:
        # Determine which package of psycopg2 is installed
        # Right now there are two packages, psycopg2 and psycopg2-binary
        # The latter is a binary wheel package that does not require a compiler
        try:
            distribution("psycopg2")
            return (_instruments_psycopg2,)
        except PackageNotFoundError:
            pass

        try:
            distribution("psycopg2-binary")
            return (_instruments_psycopg2_binary,)
        except PackageNotFoundError:
            pass

        return _instruments_any

    def _instrument(self, **kwargs):
        """Integrate with PostgreSQL Psycopg library.
        Psycopg: http://initd.org/psycopg/
        """
        tracer_provider = kwargs.get("tracer_provider")
        enable_sqlcommenter = kwargs.get("enable_commenter", False)
        commenter_options = kwargs.get("commenter_options", {})
        meter_provider = kwargs.get("meter_provider")
        enable_attribute_commenter = kwargs.get(
            "enable_attribute_commenter", False
        )
        dbapi.wrap_connect(
            __name__,
            psycopg2,
            "connect",
            self._DATABASE_SYSTEM,
            self._CONNECTION_ATTRIBUTES,
            version=__version__,
            tracer_provider=tracer_provider,
            db_api_integration_factory=DatabaseApiIntegration,
            enable_commenter=enable_sqlcommenter,
            commenter_options=commenter_options,
            enable_attribute_commenter=enable_attribute_commenter,
        )
        _OpenTelemetrySemanticConventionStability._initialize()
        sem_conv_opt_in_mode = _OpenTelemetrySemanticConventionStability._get_opentelemetry_stability_opt_in_mode(
            _OpenTelemetryStabilitySignalType.DATABASE,
        )
        meter = get_meter(
            __name__,
            __version__,
            meter_provider=meter_provider,
            schema_url=_get_schema_url(sem_conv_opt_in_mode),
        )

        Psycopg2Instrumentor._duration_histogram_db_operation = meter.create_histogram(
            DB_CLIENT_OPERATION_DURATION,
            unit="s",
            description="Duration of database client operations",
        )
        Psycopg2Instrumentor._error_execution_statements_count = meter.create_counter(
            "db.client.error.execution.statements.count",
            unit="by",
            description="Error execution statements count for database client operations",
        )
        Psycopg2Instrumentor._execution_statements_count = meter.create_counter(
            "db.client.execution.statements.count",
            unit="by",
            description="Execution statements count for database client operations",
        )
        
    def _uninstrument(self, **kwargs):
        """ "Disable Psycopg2 instrumentation"""
        dbapi.unwrap_connect(psycopg2, "connect")

    # TODO(owais): check if core dbapi can do this for all dbapi implementations e.g, pymysql and mysql
    @staticmethod
    def instrument_connection(connection, tracer_provider=None):
        """Enable instrumentation in a psycopg2 connection.

        Args:
            connection: psycopg2.extensions.connection
                The psycopg2 connection object to be instrumented.
            tracer_provider: opentelemetry.trace.TracerProvider, optional
                The TracerProvider to use for instrumentation. If not specified,
                the global TracerProvider will be used.

        Returns:
            An instrumented psycopg2 connection object.
        """

        if not hasattr(connection, "_is_instrumented_by_opentelemetry"):
            connection._is_instrumented_by_opentelemetry = False

        if not connection._is_instrumented_by_opentelemetry:
            setattr(
                connection, _OTEL_CURSOR_FACTORY_KEY, connection.cursor_factory
            )
            connection.cursor_factory = _new_cursor_factory(
                tracer_provider=tracer_provider
            )
            connection._is_instrumented_by_opentelemetry = True
        else:
            _logger.warning(
                "Attempting to instrument Psycopg connection while already instrumented"
            )
        return connection

    # TODO(owais): check if core dbapi can do this for all dbapi implementations e.g, pymysql and mysql
    @staticmethod
    def uninstrument_connection(connection):
        connection.cursor_factory = getattr(
            connection, _OTEL_CURSOR_FACTORY_KEY, None
        )

        return connection


# TODO(owais): check if core dbapi can do this for all dbapi implementations e.g, pymysql and mysql
class DatabaseApiIntegration(dbapi.DatabaseApiIntegration):
    def wrapped_connection(
        self,
        connect_method: typing.Callable[..., typing.Any],
        args: typing.Tuple[typing.Any, typing.Any],
        kwargs: typing.Dict[typing.Any, typing.Any],
    ):
        """Add object proxy to connection object."""
        base_cursor_factory = kwargs.pop("cursor_factory", None)
        new_factory_kwargs = {"db_api": self}
        if base_cursor_factory:
            new_factory_kwargs["base_factory"] = base_cursor_factory
        kwargs["cursor_factory"] = _new_cursor_factory(**new_factory_kwargs)
        connection = connect_method(*args, **kwargs)
        self.get_connection_attributes(connection)
        return connection

class CursorTracer(dbapi.CursorTracer):
    def get_operation_name(self, cursor, args):
        if not args:
            return ""

        statement = args[0]
        if isinstance(statement, Composed):
            statement = statement.as_string(cursor)

        if isinstance(statement, str):
            # Strip leading comments so we get the operation name.
            return self._leading_comment_remover.sub("", statement).split()[0]

        return ""

    def get_statement(self, cursor, args):
        if not args:
            return ""

        statement = args[0]
        if isinstance(statement, Composed):
            statement = statement.as_string(cursor)
        return statement

    def traced_execution(
        self,
        cursor: CursorT,
        query_method: typing.Callable[..., typing.Any],
        *args: tuple[typing.Any, ...],
        **kwargs: dict[typing.Any, typing.Any],
    ):
        start = default_timer()
        attrs = {
            SpanAttributes.DB_SYSTEM: self._db_api_integration.database_system,
            SpanAttributes.DB_NAME:  self._db_api_integration.database,
        }
        try:
            return super().traced_execution(cursor, query_method, *args, **kwargs)
        except Exception as e:
            counter_err = Psycopg2Instrumentor.error_execution_statements_count
            if counter_err is not None:
                attr_error = copy.deepcopy(attrs)
                attr_error[SpanAttributes.EXCEPTION_TYPE] = type(e).__name__
                attr_error[SpanAttributes.EXCEPTION_MESSAGE] = str(e)
                counter_err.add(1, attr_error)
        finally:
            dur = max(default_timer() - start, 0.0)
            hist = Psycopg2Instrumentor._duration_histogram_db_operation
            counter_exec = Psycopg2Instrumentor._execution_statements_count
            if counter_exec is not None:
                counter_exec.add(1, attrs)
            if hist is not None:
                attr_histogram = copy.deepcopy(attrs)
                statement = self.get_statement(cursor, args)
                attr_histogram[SpanAttributes.DB_STATEMENT] = statement
                hist.record(dur, attr_histogram)



def _new_cursor_factory(db_api=None, base_factory=None, tracer_provider=None):
    if not db_api:
        db_api = DatabaseApiIntegration(
            __name__,
            Psycopg2Instrumentor._DATABASE_SYSTEM,
            connection_attributes=Psycopg2Instrumentor._CONNECTION_ATTRIBUTES,
            version=__version__,
            tracer_provider=tracer_provider,
        )

    base_factory = base_factory or pg_cursor
    _cursor_tracer = CursorTracer(db_api)

    class TracedCursorFactory(base_factory):
        def execute(self, *args, **kwargs):
            return _cursor_tracer.traced_execution(
                self, super().execute, *args, **kwargs
            )

        def executemany(self, *args, **kwargs):
            return _cursor_tracer.traced_execution(
                self, super().executemany, *args, **kwargs
            )

        def callproc(self, *args, **kwargs):
            return _cursor_tracer.traced_execution(
                self, super().callproc, *args, **kwargs
            )

    return TracedCursorFactory
