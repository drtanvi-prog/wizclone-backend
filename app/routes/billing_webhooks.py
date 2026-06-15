from fastapi import APIRouter, Request, HTTPException
import json
from app.core.database import db

router = APIRouter(prefix="/api/webhooks", tags=["Billing Webhooks"])

@router.post("/app-events")
async def handle_app_events(request: Request):
    """
    Catches ALL app lifecycle and billing events from monday.com
    (e.g., app_subscription_created, uninstall)
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    print("\n[billing webhook] ┌── Received monday.com App Event!")
    print(f"[billing webhook] │ Payload: {json.dumps(body, indent=2)}")

    # Monday sends a 'challenge' on first setup
    if "challenge" in body:
        print("[billing webhook] └── Replying to challenge.")
        return {"challenge": body["challenge"]}

    event_type = body.get("type")
    
    # Extract data from the monday.com payload
    event_data = body.get("data", {})
    account_id = event_data.get("account_id")
    monday_plan_slug = event_data.get("plan_id") # Monday's string ID (e.g. 'pro')
    
    # Example structure for monday.com billing events
    if event_type == "app_subscription_created":
        print("[billing webhook] │ Subscription Created!")
        # Find the workspace ID using the monday account ID
        if account_id:
            try:
                ws_res = db.table("workspaces").select("id").eq("monday_account_id", str(account_id)).limit(1).execute()
                if ws_res.data:
                    workspace_uuid = ws_res.data[0]["id"]
                    
                    # 1. Lookup the UUID for this plan from the 'plans' table
                    plan_res = db.table("plans").select("id").ilike("plan_name", str(monday_plan_slug)).limit(1).execute()
                    
                    if plan_res.data:
                        db_plan_uuid = plan_res.data[0]["id"]
                        
                        # 2. Upsert into workspace_subscriptions using the EXACT schema!
                        db.table("workspace_subscriptions").upsert({
                            "workspace_id":    workspace_uuid,
                            "plan_id":         db_plan_uuid, # Must be the UUID!
                            "billing_status":  "ACTIVE",
                            "is_active":       True
                        }).execute()
                        
                        # 3. CRITICAL FIX: Sync the plan_tier on the workspaces table so workers know!
                        db.table("workspaces").update({
                            "plan_tier": str(monday_plan_slug).upper() # Enums are uppercase
                        }).eq("id", workspace_uuid).execute()
                        
                        print(f"[billing webhook] │ Updated subscription for workspace {workspace_uuid}")
                    else:
                        print(f"[billing webhook] │ ERROR: Plan '{monday_plan_slug}' not found in 'plans' table!")
                        
            except Exception as e:
                print(f"[billing webhook] │ DB Error: {e}")
                
    elif event_type == "app_subscription_renewed":
        print("[billing webhook] │ Subscription Renewed!")
        
    elif event_type == "app_subscription_cancelled":
        print("[billing webhook] │ Subscription Cancelled :(")
        if account_id:
            try:
                ws_res = db.table("workspaces").select("id").eq("monday_account_id", str(account_id)).limit(1).execute()
                if ws_res.data:
                    workspace_uuid = ws_res.data[0]["id"]
                    # If they keep PRO status until period ends, we shouldn't downgrade `workspaces.plan_tier` here.
                    # A cron job should downgrade them to "FREE" when the period finishes.
                    db.table("workspace_subscriptions").update({
                        "billing_status": "CANCELLED",
                        "is_active": False
                    }).eq("workspace_id", workspace_uuid).execute()
            except Exception as e:
                pass
        
    elif event_type == "uninstall":
        print("[billing webhook] │ App Uninstalled")

    print("[billing webhook] └── Done processing event.")
    
    # Must return 200 OK fast so Monday doesn't timeout
    return {"success": True}
