# app/core/database.py
# ─────────────────────────────────────────────────────────────
# Supabase client — single shared admin instance
#
# WHY ONE INSTANCE?
# Creating a new client on every request is expensive.
# supabase_admin is created once at startup and reused everywhere.
#
# HOW TO USE:
#   from app.core.database import db
#   result = db.table("workspaces").select("*").execute()
#
# In FastAPI routes that need Depends():
#   from app.core.database import get_db
#   async def my_route(db = Depends(get_db)): ...
#
# In plain helper functions (NOT FastAPI endpoints):
#   from app.core.database import db
#   result = db.table(...).execute()
# ─────────────────────────────────────────────────────────────

from supabase import create_client, Client
from app.core.config import settings

# ── Single shared admin client ──
# Service role key bypasses Row Level Security (RLS)
# Used for all backend operations
db_url = settings.supabase_url if settings.supabase_url else "https://dummy.supabase.co"
db_key = settings.supabase_service_role_key if settings.supabase_service_role_key else "dummy_key"

db: Client = create_client(
    db_url,
    db_key,
)


def get_db() -> Client:
    """
    FastAPI dependency — inject db into route handlers.
    Usage:  async def route(db = Depends(get_db))
    """
    return db