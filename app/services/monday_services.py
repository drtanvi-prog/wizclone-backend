# app/services/monday_services.py
# ─────────────────────────────────────────────────────────────
# monday.com GraphQL API helpers
#
# All calls to monday.com live here.
# Routes and workers import from this file — never call the
# monday API directly from route handlers.
# ─────────────────────────────────────────────────────────────

import httpx
import asyncio
from app.core.config import settings

MONDAY_API_URL = settings.monday_api_url


# ─────────────────────────────────────────
# User / workspace info
# ─────────────────────────────────────────

async def get_user_info(access_token: str) -> dict:
    """
    Fetch the current user's profile and account info from monday.com.
    Used in /api/auth/init to save workspace + user to DB.

    Returns the raw monday.com response dict.
    Raises httpx.HTTPError on failure.
    """
    query = """
    query {
      me {
        id
        name
        email
        account 
        { 
          id  
          name 
        }
      }
    }
    """
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            MONDAY_API_URL,
            json={"query": query},
            headers={
                "Authorization": access_token,
                "Content-Type":  "application/json",
            },
        )

    # Raise error if request fails
    response.raise_for_status()

    # Return user data
    return response.json()


# ─────────────────────────────────────────
# Subitem creation
# ─────────────────────────────────────────

async def create_subitem(
    parent_item_id: int,
    subitem_name:   str,
    access_token:   str,
) -> dict:
    """
    Create a single subitem under a parent item in monday.com.

    Returns:
        {"success": True,  "subitem_id": "123"}   on success
        {"success": False, "error": "..."}         on failure

    Never raises — worker loops call this per-subitem and
    a single failure must not stop the rest.
    """
    mutation = """
    mutation CreateSubitem($parentId: ID!, $name: String!) {
      create_subitem(parent_item_id: $parentId, item_name: $name) {
        id
        name
      }
    }
    """
    
    retry_delays = [1, 4, 16]
    attempt = 0
    
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    MONDAY_API_URL,
                    json={
                        "query":     mutation,
                        "variables": {
                            "parentId": str(parent_item_id),
                            "name":     subitem_name,
                        },
                    },
                    headers={
                        "Authorization": access_token,
                        "Content-Type":  "application/json",
                        "API-Version":   "2024-01",
                    },
                )

            # Check for rate limiting or server errors to retry
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < len(retry_delays):
                    await asyncio.sleep(retry_delays[attempt])
                    attempt += 1
                    continue
                else:
                    return {"success": False, "error": f"HTTP {response.status_code} after retries"}

            if response.status_code != 200:
                return {"success": False, "error": f"HTTP {response.status_code}"}

            data = response.json()

            if "errors" in data:
                return {"success": False, "error": str(data["errors"])}

            subitem_id = data.get("data", {}).get("create_subitem", {}).get("id")
            if not subitem_id:
                return {"success": False, "error": "No subitem ID returned"}

            return {"success": True, "subitem_id": subitem_id}

        except Exception as e:
            if attempt < len(retry_delays):
                await asyncio.sleep(retry_delays[attempt])
                attempt += 1
                continue
            else:
                return {"success": False, "error": str(e)}

async def create_subitems_batch(
    parent_item_id: int,
    subitem_names:  list[str],
    access_token:   str,
) -> dict:
    """
    Create multiple subitems under a parent item using a batched GraphQL query.
    Chunks requests into sizes of 20 to avoid exceeding Monday.com complexity limits.
    Returns:
        {
            "success": True/False,
            "copied_count": int,
            "failed_count": int,
            "failed_names": list[str],
            "token_revoked": bool
        }
    """
    result = {
        "success": True,
        "copied_count": 0,
        "failed_count": 0,
        "failed_names": [],
        "token_revoked": False
    }

    if not subitem_names:
        return result

    CHUNK_SIZE = 20
    retry_delays = [1, 4, 16]

    for i in range(0, len(subitem_names), CHUNK_SIZE):
        chunk = subitem_names[i : i + CHUNK_SIZE]
        
        # Build dynamic query
        mutation_parts = []
        variables = {"parentId": str(parent_item_id)}
        
        for idx, name in enumerate(chunk):
            var_name = f"name{idx}"
            mutation_parts.append(
                f"sub{idx}: create_subitem(parent_item_id: $parentId, item_name: ${var_name}) {{ id }}"
            )
            variables[var_name] = name

        var_defs = ", ".join(f"$name{idx}: String!" for idx in range(len(chunk)))
        mutation = f"mutation CreateBatchSubitems($parentId: ID!, {var_defs}) {{ {' '.join(mutation_parts)} }}"
        
        attempt = 0
        chunk_success = False
        
        while attempt <= len(retry_delays):
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.post(
                        MONDAY_API_URL,
                        json={
                            "query": mutation,
                            "variables": variables,
                        },
                        headers={
                            "Authorization": access_token,
                            "Content-Type":  "application/json",
                            "API-Version":   "2024-01",
                        },
                    )

                if response.status_code == 401:
                    result["success"] = False
                    result["token_revoked"] = True
                    result["failed_count"] += len(chunk)
                    result["failed_names"].extend([f"{n} (token revoked)" for n in chunk])
                    return result

                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < len(retry_delays):
                        await asyncio.sleep(retry_delays[attempt])
                        attempt += 1
                        continue
                    else:
                        result["success"] = False
                        result["failed_count"] += len(chunk)
                        result["failed_names"].extend([f"{n} (HTTP {response.status_code})" for n in chunk])
                        break

                if response.status_code != 200:
                    result["success"] = False
                    result["failed_count"] += len(chunk)
                    result["failed_names"].extend([f"{n} (HTTP {response.status_code})" for n in chunk])
                    break

                data = response.json()
                if "errors" in data and not data.get("data"):
                    # Total failure of query
                    result["success"] = False
                    result["failed_count"] += len(chunk)
                    result["failed_names"].extend([f"{n} (GraphQL Error)" for n in chunk])
                    break
                
                # Check individual aliases
                res_data = data.get("data", {})
                errors = data.get("errors", [])
                
                for idx, name in enumerate(chunk):
                    alias = f"sub{idx}"
                    if res_data and res_data.get(alias) and res_data[alias].get("id"):
                        result["copied_count"] += 1
                    else:
                        result["success"] = False
                        result["failed_count"] += 1
                        result["failed_names"].append(f"{name} (failed)")

                chunk_success = True
                break

            except Exception as e:
                if attempt < len(retry_delays):
                    await asyncio.sleep(retry_delays[attempt])
                    attempt += 1
                    continue
                else:
                    result["success"] = False
                    result["failed_count"] += len(chunk)
                    result["failed_names"].extend([f"{n} ({str(e)})" for n in chunk])
                    break
                    
        if not chunk_success and result["token_revoked"]:
            break

    return result

async def get_item_subitems(item_id: int, access_token: str) -> list[str]:
    """Fetch subitems of an item to check for manual additions."""
    query = """
    query GetSubitems($itemId: [ID!]) {
      items(ids: $itemId) {
        subitems {
          name
        }
      }
    }
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                MONDAY_API_URL,
                json={"query": query, "variables": {"itemId": [str(item_id)]}},
                headers={
                    "Authorization": access_token,
                    "Content-Type":  "application/json",
                    "API-Version":   "2024-01",
                },
            )
        data = response.json()
        items = data.get("data", {}).get("items", [])
        if not items:
            return []
        subitems = items[0].get("subitems", [])
        return [sub["name"] for sub in subitems]
    except Exception as e:
        print(f"[monday_services] get_item_subitems error: {e}")
        return []