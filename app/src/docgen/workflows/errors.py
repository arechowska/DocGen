from docgen.jobs.runner import UserSafeJobError


class WorkflowError(UserSafeJobError):
    """A workflow validation failure safe to show to the user."""
