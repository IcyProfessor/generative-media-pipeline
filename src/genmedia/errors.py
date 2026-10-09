"""Short, safe errors: do not embed SDK responses, credentials or signed URLs."""

class PipelineError(Exception):
    pass

class QualityError(PipelineError):
    """Deterministic contract violation; do not regenerate paid audio automatically."""

class ProviderError(PipelineError):
    pass

class SubmissionRejected(ProviderError):
    """Explicit refusal/no request sent. Only this class permits resubmission."""


class SubmissionUncertain(ProviderError):
    """A paid POST may have reached the provider. Reconcile using its dashboard."""


class AlignmentUncertain(PipelineError):
    """A possibly charged alignment request needs explicit retry consent."""
