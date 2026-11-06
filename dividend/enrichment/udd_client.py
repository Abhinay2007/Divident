"""Read structured source metadata from Debian's public UDD PostgreSQL mirror."""

from collections.abc import Callable

from .base import FactSet

_QUERY = """
SELECT s.source, s.homepage,
       MAX(m.value) FILTER (WHERE m.key = 'Repository') AS repository,
       MAX(m.value) FILTER (WHERE m.key = 'Contact') AS contact
FROM (SELECT DISTINCT ON (s.source) s.source, s.homepage
      FROM sources s JOIN releases r ON r.release = s.release
      WHERE s.source = ANY(%s)
      ORDER BY s.source, r.sort DESC) s
LEFT JOIN upstream_metadata m ON m.source = s.source
GROUP BY s.source, s.homepage
"""


class UDDClient:
    def __init__(self, dsn: str, connect: Callable | None = None):
        self.dsn = dsn
        self._connect = connect

    def fetch(self, source_names: list[str]) -> dict[str, FactSet]:
        if not source_names:
            return {}
        connect = self._connect
        if connect is None:
            import psycopg
            connect = psycopg.connect
        with connect(self.dsn) as connection, connection.cursor() as cursor:
            cursor.execute(_QUERY, (source_names,))
            rows = cursor.fetchall()
        result = {name: FactSet(name, partial=True) for name in source_names}
        for source, homepage, repository, contact in rows:
            result[source] = FactSet(source, repository_url=repository, homepage=homepage,
                                     contact=contact, partial=not bool(repository or homepage or contact))
        return result
