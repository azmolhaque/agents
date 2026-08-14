"""CindraLeads — passive-only lead intelligence for Cindrasec."""

__all__ = ["PIPELINE_VERSION", "__version__"]

__version__ = "0.1.0"

# Stamped onto every Lead. Bump when a change alters scoring or extraction output.
# Prompt changes are tracked separately (PLAN.md 2.10) because pipeline_version
# does not move when only a prompt file is edited.
PIPELINE_VERSION = "0.1.0"
