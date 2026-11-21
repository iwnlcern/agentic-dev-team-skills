"""One test seam for this slice's interim project serving state."""


def serve_for_test(writer, project_id) -> None:
    writer.open_project(project_id)
