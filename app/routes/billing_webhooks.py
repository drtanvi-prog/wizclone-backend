from fastapi import APIRouter, Request, HTTPException, Body
import json
from datetime import datetime, timezone
from app.core.database import db

router = APIRouter(prefix="/api/webhooks", tags=["Billing Webhooks"])

@router.post("/app-events")
async def handle_app_events(
    body: dict = Body(..., example={
        "type": "app_subscription_created",
        "data": {
            "account_id": 28826682,
            "subscription": {
                "plan_id": "BUSINESS",
                "renewal_date": "2026-12-31T00:00:00Z"
            }
        }
    })
):
    """
    Catches ALL app lifecycle and billing events from monday.com
    (e.g., app_subscription_created, uninstall)
    """

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
    
    # Monday sends billing details inside the 'subscription' object
    subscription = event_data.get("subscription", {})
    monday_plan_slug = subscription.get("plan_id") or event_data.get("plan_id")
    renewal_date = subscription.get("renewal_date")
    

    # Group 1: Upgrades & Renewals
    if event_type in [
        "app_subscription_created", 
        "app_subscription_renewed", 
        "app_subscription_changed", 
        "app_subscription_cancellation_revoked_by_user"
    ]:
        print(f"[billing webhook] │ Handling Upgrade/Renewal: {event_type}")
        if account_id:
            try:
                ws_res = db.table("workspaces").select("id").eq("monday_account_id", str(account_id)).limit(1).execute()
                if ws_res.data:
                    workspace_uuid = ws_res.data[0]["id"]
                    plan_res = db.table("plans").select("id").ilike("plan_name", str(monday_plan_slug)).limit(1).execute()
                    if plan_res.data:
                        db_plan_uuid = plan_res.data[0]["id"]
                        
                        upsert_data = {
                            "workspace_id": workspace_uuid,
                            "plan_id": db_plan_uuid,
                            "billing_status": "ACTIVE",
                            "is_active": True
                        }
                        if renewal_date:
                            upsert_data["current_period_end"] = renewal_date
                        
                        # Safe upsert handling without primary key
                        existing_sub = db.table("workspace_subscriptions").select("id").eq("workspace_id", workspace_uuid).execute()
                        if existing_sub.data:
                            db.table("workspace_subscriptions").update(upsert_data).eq("id", existing_sub.data[0]["id"]).execute()
                        else:
                            db.table("workspace_subscriptions").insert(upsert_data).execute()
                        db.table("workspaces").update({
                            "plan_tier": str(monday_plan_slug).upper(),
                            "status": "ACTIVE",
                            "is_active": True
                        }).eq("id", workspace_uuid).execute()
                        print(f"[billing webhook] │ Updated workspace {workspace_uuid} to {str(monday_plan_slug).upper()}")
            except Exception as e:
                print(f"[billing webhook] │ DB Error: {e}")

    # Group 2: Cancellations (Pending Expiration)
    elif event_type in ["app_subscription_cancelled", "app_subscription_cancelled_by_user"]:
        print(f"[billing webhook] │ Handling Cancellation: {event_type}")
        if account_id:
            try:
                ws_res = db.table("workspaces").select("id").eq("monday_account_id", str(account_id)).limit(1).execute()
                if ws_res.data:
                    workspace_uuid = ws_res.data[0]["id"]
                    db.table("workspace_subscriptions").update({
                        "billing_status": "CANCELLED"
                    }).eq("workspace_id", workspace_uuid).execute()
            except Exception:
                pass

    # Group 3: Failures & Trial Ended (Immediate Downgrade)
    elif event_type in ["app_trial_subscription_ended", "app_subscription_renewal_failed"]:
        print(f"[billing webhook] │ Handling Immediate Downgrade: {event_type}")
        if account_id:
            try:
                ws_res = db.table("workspaces").select("id").eq("monday_account_id", str(account_id)).limit(1).execute()
                if ws_res.data:
                    workspace_uuid = ws_res.data[0]["id"]
                    db.table("workspace_subscriptions").update({
                        "billing_status": "CANCELLED"
                    }).eq("workspace_id", workspace_uuid).execute()
                    db.table("workspaces").update({
                        "plan_tier": "FREE"
                    }).eq("id", workspace_uuid).execute()
            except Exception:
                pass

    # Group 4: Trials & Past Due
    elif event_type == "app_trial_subscription_started":
        print(f"[billing webhook] │ Handling Trial Started")
        if account_id:
            try:
                ws_res = db.table("workspaces").select("id").eq("monday_account_id", str(account_id)).limit(1).execute()
                if ws_res.data:
                    workspace_uuid = ws_res.data[0]["id"]
                    plan_res = db.table("plans").select("id").ilike("plan_name", "PRO").limit(1).execute()
                    if plan_res.data:
                        upsert_data = {
                            "workspace_id": workspace_uuid,
                            "plan_id": plan_res.data[0]["id"],
                            "billing_status": "TRIAL",
                            "is_active": True
                        }
                        existing_sub = db.table("workspace_subscriptions").select("id").eq("workspace_id", workspace_uuid).execute()
                        if existing_sub.data:
                            db.table("workspace_subscriptions").update(upsert_data).eq("id", existing_sub.data[0]["id"]).execute()
                        else:
                            db.table("workspace_subscriptions").insert(upsert_data).execute()
                            
                        db.table("workspaces").update({
                            "plan_tier": "PRO"
                        }).eq("id", workspace_uuid).execute()
            except Exception:
                pass
                
    elif event_type == "app_subscription_renewal_attempt_failed":
        print(f"[billing webhook] │ Handling Past Due")
        if account_id:
            try:
                ws_res = db.table("workspaces").select("id").eq("monday_account_id", str(account_id)).limit(1).execute()
                if ws_res.data:
                    db.table("workspace_subscriptions").update({
                        "billing_status": "PAST_DUE"
                    }).eq("workspace_id", ws_res.data[0]["id"]).execute()
            except Exception:
                pass

    # Group 5: Uninstall / Install
    elif event_type == "uninstall":
        print("[billing webhook] │ App Uninstalled")
        if account_id:
            try:
                db.table("workspaces").update({
                    "status": "UNINSTALLED",
                    "is_active": False,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }).eq("monday_account_id", str(account_id)).execute()
            except Exception:
                pass
                
    elif event_type == "install":
        print("[billing webhook] │ App Installed (Handled by OAuth)")
        
    else:
        print(f"[billing webhook] │ Ignored Event: {event_type}")

    print("[billing webhook] └── Done processing event.")
    
    # Must return 200 OK fast so Monday doesn't timeout
    return {"success": True}
