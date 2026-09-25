"""Microsoft Graph stand-ins for the RECORDING_LINK tests. No network, ever."""
from app.graph.transport import GraphError


class FakeGraph:
    """Answers GET collections by path; records every call; can raise."""

    def __init__(self, collections=None, errors=None, json=None, posts=None):
        self.collections = collections or {}
        self.errors = errors or {}
        self.json = json or {}
        self.posts = posts or {}
        self.paths = []

    def _raise_if(self, path):
        for prefix, error in self.errors.items():
            if path.startswith(prefix):
                raise error

    def get_collection(self, path):
        self.paths.append(("GET", path))
        self._raise_if(path)
        for prefix, rows in self.collections.items():
            if path.startswith(prefix):
                return rows
        return []

    def get_json(self, path):
        self.paths.append(("GET", path))
        self._raise_if(path)
        return self.json[path]

    def post_json(self, path, body):
        self.paths.append(("POST", path))
        self._raise_if(path)
        return self.posts.get(path, {})


def graph_error(status, code="Forbidden", message="denied"):
    return GraphError("Microsoft Graph", status, code, message)
