from django.db import connection


CHANNEL = "plane_codex"


def wake():
    # PostgreSQL delivers NOTIFY only after the transaction commits.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_notify(%s, '')", [CHANNEL])


def database_params():
    database = connection.settings_dict
    params = {
        target: database[source]
        for source, target in (
            ("NAME", "dbname"),
            ("USER", "user"),
            ("PASSWORD", "password"),
            ("HOST", "host"),
            ("PORT", "port"),
        )
        if database.get(source)
    }
    params.update(
        {
            key: value
            for key, value in database.get("OPTIONS", {}).items()
            if key in ("sslmode", "sslrootcert", "sslcert", "sslkey")
        }
    )
    return params
