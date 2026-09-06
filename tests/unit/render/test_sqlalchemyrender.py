import datetime as dt
import sqlite3
from contextlib import closing
from textwrap import dedent

import duckdb
import pytest

from mindsdb_sql_parser.ast import (
    Identifier,
    Select,
    Star,
    Constant,
    Tuple,
    BinaryOperation,
    CreateTable,
    TableColumn,
    Insert,
)
from mindsdb_sql_parser import parse_sql
from mindsdb.interfaces.query_context.last_query import LastQuery
from mindsdb.utilities.render.sqlalchemy_render import SqlalchemyRender


class TestMysqlRender:
    def test_create_table(self):
        query = CreateTable(
            name="tbl1",
            columns=[
                TableColumn(name="a", type="DATE"),
                TableColumn(name="b", type="INTEGER"),
            ],
        )

        sql = SqlalchemyRender("mysql").get_string(query, with_failback=False)

        sql2 = """CREATE TABLE tbl1 (a DATE, b INTEGER)"""

        assert sql.replace("\n", "").replace("\t", "").replace("  ", " ") == sql2

    def test_datetype(self):
        query = Select(targets=[Constant(value=dt.datetime(2011, 1, 1))])

        sql = SqlalchemyRender("mysql").get_string(query, with_failback=False)

        sql2 = """SELECT '2011-01-01 00:00:00' AS `2011-01-01 00:00:00`"""
        assert sql == sql2

        query = Select(
            targets=[Star()],
            from_table=Identifier("tb1"),
            where=BinaryOperation(
                op="in",
                args=[
                    Identifier("x"),
                    Tuple(items=[Constant(value=dt.datetime(2011, 1, 1)), Constant(value=dt.datetime(2011, 1, 2))]),
                ],
            ),
        )
        sql = SqlalchemyRender("mysql").get_string(query, with_failback=False)

        sql2 = """SELECT * FROM tb1 WHERE x IN ('2011-01-01 00:00:00', '2011-01-02 00:00:00')"""
        assert sql.replace("\n", "").replace("\t", "").replace("  ", " ") == sql2

    def test_exec_params(self):
        values = [
            [1, "2"],
            [3, "b"],
        ]

        query = Insert(
            table=Identifier("tbl1"),
            columns=[
                Identifier("a"),
                Identifier("b"),
            ],
            values=values,
            is_plain=True,
        )

        sql, params = SqlalchemyRender("mysql").get_exec_params(query, with_failback=False)

        assert sql == """INSERT INTO tbl1 (a, b) VALUES (%s, %s)"""
        assert params == values


class TestPostgresRender:
    def test_alias_in_case(self):
        sql = """
           select case mean when 0 then null else stdev/mean end cov from table1
        """

        query = parse_sql(sql)
        rendered = SqlalchemyRender("postgres").get_string(query, with_failback=False)

        # check queries are the same after render
        assert str(query) == str(parse_sql(rendered))

    def test_extra_cast_in_division(self):
        sql = """
           select a / b as col1 from table1
        """

        query = parse_sql(sql)
        rendered = SqlalchemyRender("postgres").get_string(query, with_failback=False)

        # check queries are the same after render
        assert str(query) == str(parse_sql(rendered))

    def test_quoted_mixed_case(self):
        query = Select(targets=[Identifier("Test", alias=Identifier("Test2"))])
        rendered = SqlalchemyRender("postgres").get_string(query, with_failback=False)
        assert rendered == "SELECT Test AS Test2"

        query = Select(targets=[Identifier("table")])
        rendered = SqlalchemyRender("postgres").get_string(query, with_failback=False)
        assert rendered == 'SELECT "table"'

    def test_star_in_path(self):
        sql = "select t.* from table t"

        query = parse_sql(sql)
        rendered = SqlalchemyRender("postgres").get_string(query, with_failback=False)

        # check queries are the same after render
        assert str(query) == str(parse_sql(rendered))

    def test_div(self):
        sql0 = "select 1 / 2 - (9 / 4 - 1) * 3 as x"
        query = parse_sql(sql0)

        sql = SqlalchemyRender("postgres").get_string(query, with_failback=False)

        assert sql.lower() == sql0

    def test_quoted_identifier(self):
        sql = "SELECT `A`.*, A.`B` AS `Bb`, `c` as Cc FROM Tbl.`Tab` AS `Tt`"

        query = parse_sql(sql)
        rendered = SqlalchemyRender("postgres").get_string(query, with_failback=False)

        # check queries are the same after render
        assert rendered.replace("\n", "") == 'SELECT "A".*, A."B" AS "Bb", "c" AS Cc FROM Tbl."Tab" AS "Tt"'

    def test_intersect_except(self):
        for op in ("EXCEPT", "INTERSECT"):
            sql = dedent(f"""
            SELECT * FROM tbl1
            {op} SELECT * FROM tbl2
            """).strip()

            query = parse_sql(sql)
            rendered = SqlalchemyRender("postgres").get_string(query, with_failback=False)

            assert rendered.replace("\n", "") == sql.replace("\n", " ")

    def test_in_with_single_value(self):
        sql = "SELECT * FROM tbl1 WHERE x IN (1)"
        query = parse_sql(sql)
        rendered = SqlalchemyRender("postgres").get_string(query, with_failback=False)

        assert rendered.replace("\n", "") == sql

    def test_join(self):
        sql = """
            SELECT * FROM tbl1
            {JOIN} tbl2 ON tbl1.x = tbl2.x
        """
        for input_join_type, output_join_type in [
            ("JOIN", "JOIN"),
            ("INNER JOIN", "JOIN"),
            ("LEFT JOIN", "LEFT OUTER JOIN"),
            ("LEFT OUTER JOIN", "LEFT OUTER JOIN"),
            # ('RIGHT JOIN', 'RIGHT OUTER JOIN'),
            # ('RIGHT OUTER JOIN', 'RIGHT OUTER JOIN'),
        ]:
            original_query = sql.format(JOIN=input_join_type)
            query = parse_sql(original_query)
            rendered = SqlalchemyRender("postgres").get_string(query, with_failback=False)
            assert " ".join(rendered.split()) == " ".join(sql.format(JOIN=output_join_type).split())

    def test_mixed_join(self):
        sql = """
            SELECT * FROM tbl1
            join tbl2 on tbl1.x = tbl2.x,
            tbl3
        """
        query = parse_sql(sql)
        rendered = SqlalchemyRender("postgres").get_string(query, with_failback=False)

        expected = dedent("""
            SELECT * FROM tbl1
            JOIN tbl2 ON tbl1.x = tbl2.x
            JOIN tbl3 ON 1=1
        """).strip()

        assert rendered.replace("\n", "") == expected.replace("\n", " ")

    def test_group_by_rollup(self):
        # test statements wth GROUP BY ROLLUP
        sql = "SELECT * FROM tbl1 GROUP BY a, b WITH ROLLUP"
        ast = parse_sql(sql)

        assert ast.group_by[-1].with_rollup is True

        rendered = SqlalchemyRender("postgres").get_string(ast, with_failback=False)
        expected = "SELECT * FROM tbl1 GROUP BY ROLLUP(a, b)"
        assert rendered.replace("\n", "").replace("  ", " ").upper() == expected.upper()

        rendered = SqlalchemyRender("mysql").get_string(ast, with_failback=False)
        expected = "SELECT * FROM tbl1 GROUP BY a, b WITH ROLLUP"
        assert rendered.replace("\n", "").replace("  ", " ").upper() == expected.upper()

        # renderer for 'oracle' is not explicetly specified - should be rollup() in result
        rendered = SqlalchemyRender("oracle").get_string(ast, with_failback=False)
        expected = "SELECT * FROM tbl1 GROUP BY ROLLUP(a, b)"
        assert rendered.replace("\n", "").replace("  ", " ").upper() == expected.upper()

        # try query with differ ending
        sql = "SELECT * FROM tbl1 GROUP BY a, b WITH ROLLUP LIMIT 100"
        ast = parse_sql(sql)

        assert ast.group_by[-1].with_rollup is True

        rendered = SqlalchemyRender("postgres").get_string(ast, with_failback=False)
        expected = "SELECT * FROM tbl1 GROUP BY ROLLUP(a, b) LIMIT 100"
        assert rendered.replace("\n", "").replace("  ", " ").upper() == expected.upper()


class TestMSSQLRender:
    def test_mixed_join(self):
        sql = """
            select * from car_info order by year limit 10 offset 1
        """
        query = parse_sql(sql)
        rendered = SqlalchemyRender("mssql").get_string(query, with_failback=False)

        expected = dedent("""
           SELECT * FROM car_info ORDER BY year
           OFFSET 1 ROWS FETCH FIRST 10 ROWS ONLY
        """).strip()

        assert rendered.replace("\n", "") == expected.replace("\n", " ")


class TestNullPredicateRendering:
    """Regression tests: negated null predicates must not lose their NOT.

    Wrapping an un-aliased NULL constant in a Label turns `x IS NULL` into a
    bind-param comparison, which defeats SQLAlchemy's negate optimization, so
    `NOT (x IS NULL)` compiled identical to `x IS NULL` (issue #12491).
    """

    def test_not_is_null_is_preserved(self):
        rendered = SqlalchemyRender("mysql").get_string(
            parse_sql("SELECT * FROM t WHERE NOT (x IS NULL)"), with_failback=False
        )
        assert str(parse_sql(rendered)) == str(parse_sql("SELECT * FROM t WHERE NOT (x IS NULL)"))

    def test_not_is_not_null_is_preserved(self):
        rendered = SqlalchemyRender("mysql").get_string(
            parse_sql("SELECT * FROM t WHERE NOT (x IS NOT NULL)"), with_failback=False
        )
        assert str(parse_sql(rendered)) == str(parse_sql("SELECT * FROM t WHERE NOT (x IS NOT NULL)"))

    def test_not_is_null_without_parens_is_preserved(self):
        rendered = SqlalchemyRender("mysql").get_string(
            parse_sql("SELECT * FROM t WHERE NOT x IS NULL"), with_failback=False
        )
        assert str(parse_sql(rendered)) == str(parse_sql("SELECT * FROM t WHERE NOT (x IS NULL)"))

    def test_positive_null_predicates_unchanged(self):
        for sql in (
            "SELECT * FROM t WHERE x IS NULL",
            "SELECT * FROM t WHERE x IS NOT NULL",
        ):
            rendered = SqlalchemyRender("mysql").get_string(parse_sql(sql), with_failback=False)
            assert str(parse_sql(rendered)) == str(parse_sql(sql))

    def test_aliased_null_in_select_keeps_label(self):
        rendered = SqlalchemyRender("mysql").get_string(parse_sql("SELECT NULL AS nothing FROM t"), with_failback=False)
        assert "AS nothing" in rendered

    def test_injected_null_constant_in_comparison(self):
        # LastQuery/update_step replace `last` and subselect params with
        # Constant(None) placeholders before injecting values; under comparison
        # operators those must render as plain NULL, not raise ArgumentError
        # (which would silently failback to the invalid `a > None`).
        query = parse_sql("SELECT * FROM t WHERE x > 0")
        query.where.args[1] = Constant(value=None)
        rendered = SqlalchemyRender("mysql").get_string(query, with_failback=False)
        assert str(parse_sql(rendered)) == str(parse_sql("SELECT * FROM t WHERE x > NULL"))

    def test_not_comparison_with_null_is_preserved(self):
        rendered = SqlalchemyRender("mysql").get_string(
            parse_sql("SELECT * FROM t WHERE NOT (x > NULL)"), with_failback=False
        )
        assert str(parse_sql(rendered)) == str(parse_sql("SELECT * FROM t WHERE x <= NULL"))

    def test_eq_null_is_not_rewritten_to_is_null(self):
        rendered = SqlalchemyRender("mysql").get_string(
            parse_sql("SELECT * FROM t WHERE x = NULL"), with_failback=False
        )
        assert str(parse_sql(rendered)) == str(parse_sql("SELECT * FROM t WHERE x = NULL"))

    def test_unaliased_null_in_select_keeps_null_label(self):
        rendered = SqlalchemyRender("mysql").get_string(parse_sql("SELECT NULL FROM t"), with_failback=False)
        assert "AS `NULL`" in rendered

    @pytest.mark.parametrize("with_failback", [False, True])
    def test_last_query_null_placeholder_and_value_injection(self, with_failback):
        query = parse_sql("SELECT * FROM tasks WHERE a > last LIMIT 1")
        last_query = LastQuery(query)
        assert isinstance(query.where.args[1], Constant)
        assert query.where.args[1].value is None
        renderer = SqlalchemyRender("postgres")
        rendered, params = renderer.get_exec_params(query, with_failback=with_failback)

        assert params is None
        assert "a > NULL" in rendered
        with duckdb.connect(":memory:") as connection:
            connection.execute("CREATE TABLE tasks(a INTEGER)")
            connection.execute("INSERT INTO tasks VALUES (1), (2), (NULL)")
            assert connection.execute(rendered).fetchall() == []

            init_query, info = next(last_query.get_init_queries())
            init_sql = renderer.get_string(init_query, with_failback=with_failback)
            assert connection.execute(init_sql).fetchall() == [(2,)]

            query = last_query.apply_values({info["table_name"]: {info["column_name"]: 2}})
            rendered = renderer.get_string(query, with_failback=with_failback)
            assert connection.execute(rendered).fetchall() == []
            connection.execute("INSERT INTO tasks VALUES (3)")
            assert connection.execute(rendered).fetchall() == [(3,)]


class TestBooleanPredicateRendering:
    @pytest.mark.parametrize("dialect", ["mysql", "postgres", "sqlite"])
    @pytest.mark.parametrize("value", ["TRUE", "FALSE"])
    @pytest.mark.parametrize(
        "predicate, expected",
        [
            ("NOT (x IS {value})", "NOT (x IS {value})"),
            ("NOT x IS {value}", "NOT (x IS {value})"),
            ("NOT (x IS NOT {value})", "NOT (x IS NOT {value})"),
            ("NOT (NOT (x IS {value}))", "NOT (NOT (x IS {value}))"),
        ],
    )
    def test_negation_is_preserved(self, dialect, value, predicate, expected):
        renderer = SqlalchemyRender(dialect)
        rendered = renderer.get_string(
            parse_sql(f"SELECT * FROM t WHERE {predicate.format(value=value)}"), with_failback=False
        )
        expected_sql = f"SELECT * FROM t WHERE {expected.format(value=value)}"
        assert " ".join(rendered.split()).upper() == expected_sql.upper()

    @pytest.mark.parametrize(
        "predicate, expected",
        [
            ("NOT (x IS TRUE)", [(2,), (3,)]),
            ("NOT (x IS FALSE)", [(1,), (3,)]),
            ("NOT (x IS NOT TRUE)", [(1,)]),
            ("NOT (x IS NOT FALSE)", [(2,)]),
            ("NOT ((x IS TRUE) OR (x IS FALSE))", [(3,)]),
            ("NOT ((id > 1) IS TRUE)", [(1,)]),
            ("NOT (x IS UNKNOWN)", [(1,), (2,)]),
            ("NOT (x IS NOT UNKNOWN)", [(3,)]),
            ("NOT (x IS NULL)", [(1,), (2,)]),
            ("NOT (x IS NOT NULL)", [(3,)]),
            ("NOT (NOT (x IS TRUE))", [(1,)]),
            ("NOT (NOT (NOT (x IS TRUE)))", [(2,), (3,)]),
            ("NOT (x IS TRUE) AND id > 1", [(2,), (3,)]),
            ("NOT (x IS TRUE) OR NOT (x IS FALSE)", [(1,), (2,), (3,)]),
        ],
    )
    def test_negation_returns_correct_rows(self, predicate, expected):
        rendered = SqlalchemyRender("postgres").get_string(
            parse_sql(f"SELECT id FROM t WHERE {predicate} ORDER BY id"), with_failback=False
        )
        with duckdb.connect(":memory:") as connection:
            connection.execute("CREATE TABLE t(id INTEGER, x BOOLEAN)")
            connection.execute("INSERT INTO t VALUES (1, TRUE), (2, FALSE), (3, NULL)")
            assert connection.execute(rendered).fetchall() == expected

    def test_boolean_select_labels_are_preserved(self):
        rendered = SqlalchemyRender("postgres").get_string(
            parse_sql("SELECT TRUE, FALSE, TRUE AS yes, FALSE AS no"), with_failback=False
        )
        with duckdb.connect(":memory:") as connection:
            result = connection.execute(rendered)
            assert [column[0] for column in result.description] == ["True", "False", "yes", "no"]
            assert result.fetchall() == [(True, False, True, False)]

    @pytest.mark.parametrize("op", ["=", "!=", ">", "<"])
    def test_boolean_comparisons_are_unchanged(self, op):
        sql = f"SELECT * FROM t WHERE x {op} TRUE"
        rendered = SqlalchemyRender("postgres").get_string(parse_sql(sql), with_failback=False)
        assert str(parse_sql(rendered)) == str(parse_sql(sql))


class TestIsPredicateNegation:
    @pytest.mark.parametrize("dialect", ["mysql", "postgres", "sqlite", "mssql", "oracle"])
    @pytest.mark.parametrize("op", ["IS", "IS NOT"])
    def test_null_negation_is_explicit_across_dialects(self, dialect, op):
        sql = f"SELECT * FROM t WHERE NOT (x {op} NULL)"
        rendered = SqlalchemyRender(dialect).get_string(parse_sql(sql), with_failback=False)
        assert " ".join(rendered.split()) == sql

    @pytest.mark.parametrize("op", ["IS", "IS NOT"])
    @pytest.mark.parametrize("negations", [0, 1, 2, 3])
    @pytest.mark.parametrize(
        "left, right",
        [
            ("x", "0"),
            ("x", "1"),
            ("x", "TRUE"),
            ("x", "FALSE"),
            ("x", "'TRUE'"),
            ("x", "'abc'"),
            ("x", "y"),
            ("x", "(1 + 1)"),
            ("x", "COALESCE(y, 0)"),
            ("x", "(SELECT 1)"),
            ("NULL", "x"),
        ],
    )
    def test_sqlite_expression_operands_return_correct_rows(self, left, right, op, negations):
        # SQLite permits arbitrary expressions on either side of IS / IS NOT.
        predicate = "NOT (" * negations + f"{left} {op} {right}" + ")" * negations
        sql = f"SELECT id FROM t WHERE {predicate} ORDER BY id"
        rendered = SqlalchemyRender("sqlite").get_string(parse_sql(sql), with_failback=False)

        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE TABLE t(id INTEGER, x, y)")
            connection.executemany(
                "INSERT INTO t VALUES (?, ?, ?)",
                [
                    (1, None, None),
                    (2, 1, 1),
                    (3, 2, 1),
                    (4, "abc", "abc"),
                    (5, "xyz", "abc"),
                    (6, 0, 0),
                    (7, -1, 0),
                    (8, 0.5, 1),
                    (9, "2", 2),
                    (10, "", 0),
                ],
            )
            expected = connection.execute(sql).fetchall()
            assert connection.execute(rendered).fetchall() == expected

    @pytest.mark.parametrize("op", ["IS", "IS NOT"])
    def test_postgres_row_null_test_is_not_inverted(self, op):
        # For a mixed-null PostgreSQL row, IS NULL and IS NOT NULL are both
        # false. Swapping the operators is not equivalent to applying NOT.
        sql = f"SELECT NOT (ROW(1, NULL) {op} NULL) AS result"
        rendered = SqlalchemyRender("postgres").get_string(parse_sql(sql), with_failback=False)
        assert " ".join(rendered.split()).upper() == sql.upper()

    def test_negated_predicate_keeps_select_alias(self):
        sql = "SELECT id, NOT (x IS NULL) AS is_present FROM t ORDER BY id"
        rendered = SqlalchemyRender("postgres").get_string(parse_sql(sql), with_failback=False)
        with duckdb.connect(":memory:") as connection:
            connection.execute("CREATE TABLE t(id INTEGER, x BOOLEAN)")
            connection.execute("INSERT INTO t VALUES (1, TRUE), (2, FALSE), (3, NULL)")
            result = connection.execute(rendered)
            assert [column[0] for column in result.description] == ["id", "is_present"]
            assert result.fetchall() == [(1, True), (2, True), (3, False)]
