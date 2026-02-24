from fastapi import FastAPI, HTTPException, Body, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.responses import PlainTextResponse
from motor.motor_asyncio import AsyncIOMotorClient
from twilio.rest import Client
import os
import httpx  
import random 
from datetime import datetime, timedelta
from dotenv import load_dotenv
from bson import ObjectId
from typing import Optional # <--- NEW: Needed for optional 'mode'

load_dotenv()

# --- CONFIGURATION ---
MONGO_URI = os.getenv("MONGO_URI")
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = "whatsapp:+14155238886" 

# External Booking API URL
BOOKING_API_URL = "https://booking-and-log-service-ey.onrender.com/book-service"

# 1. Initialize FastAPI App
app = FastAPI()

@app.get("/")
async def health_check():
    """
    This route is for the cron job to keep the server awake.
    """
    return {
        "status": "online",
        "timestamp": datetime.now(),
        "message": "Messaging API is active"
    }

# 2. Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. Connect to Database
client = AsyncIOMotorClient(MONGO_URI)
db_auto = client["auto_ai_db"] 
admin_collection = db_auto.service_centers
sms_sessions = db_auto.sms_sessions
db_tech = client["techathon_db"]
vehicle_collection = db_tech.vehicles
users_collection = db_tech.users

# 5. Initialize Twilio Client
twilio_client = Client(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID else None

# --- MODELS ---
class SensorAlert(BaseModel):
    vehicle_id: str
    issue_detected: str     
    mode: Optional[str] = "manual"  # <--- NEW: Accepts mode from Flask

# --- API ENDPOINTS ---

@app.post("/sensor-anomaly")
async def sensor_anomaly_alert(alert: SensorAlert):
    if not twilio_client:
        raise HTTPException(status_code=500, detail="Twilio not configured")

    # --- 1. FETCH DETAILS ---
    print(f"🔍 DEBUG: Looking up owner for: {alert.vehicle_id} in mode: {alert.mode}")
    
    vehicle = await vehicle_collection.find_one({"vehicle_id": alert.vehicle_id.strip()})
    if not vehicle:
        return {"status": "error", "message": "Vehicle ID not registered."}
    
    user_id = vehicle.get("user_id")
    db_company_name = vehicle.get("model") 

    user = await users_collection.find_one({"user_id": user_id})
    if not user:
        return {"status": "error", "message": "Vehicle owner not found."}

    owner_phone = user.get("phone")
    
    # --- 2. DETERMINE COMPANY ---
    company_name = None
    if "_" in alert.vehicle_id:
        company_name = alert.vehicle_id.split("_")[0]
    elif db_company_name:
        company_name = db_company_name
    else:
        return {"status": "error", "message": "Could not determine company name."}

    # --- 3. AUTO MODE LOGIC ---
    if alert.mode == "auto":
        # Find just the nearest/first center for Auto
        centers_cursor = admin_collection.find({
            "company_name": {"$regex": f"^{company_name}", "$options": "i"}
        })
        centers = await centers_cursor.to_list(length=1)
        
        if not centers:
            return {"status": "warning", "message": f"No service centers found for {company_name}"}
        
        center = centers[0]
        center_id = center.get("centerId", "UnknownID")
        center_name = center.get("name", "Nearest Center")
        
        menu_text = f"🚨 *ALERT:* {alert.issue_detected} detected for {alert.vehicle_id}.\n\n"
        menu_text += f"🤖 We have auto-selected your nearest center: *{center_name}* ({center.get('location', 'Unknown')}).\n\n"
        menu_text += "Please reply with the *Date and Time* you are free for the service (e.g., 'Tomorrow at 10 AM', 'Oct 25 at 2 PM')."

        await sms_sessions.update_one(
            {"phone": owner_phone}, 
            {"$set": {
                "vehicle_id": alert.vehicle_id,
                "user_id": user_id,
                "issue": alert.issue_detected,
                "auto_center_id": center_id, # Store auto-selected center
                "state": "WAITING_FOR_DATETIME", # <--- NEW: State tracking
                "timestamp": datetime.now()
            }},
            upsert=True 
        )

    # --- 4. MANUAL MODE LOGIC (Original) ---
    else:
        centers_cursor = admin_collection.find({
            "company_name": {"$regex": f"^{company_name}", "$options": "i"}
        })
        centers = await centers_cursor.to_list(length=5)

        if not centers:
            return {"status": "warning", "message": f"No service centers found for {company_name}"}

        menu_text = f"🚨 *ALERT:* {alert.issue_detected} detected for {alert.vehicle_id}.\n\n"
        menu_text += f"Select a {company_name} Service Center:\n"
        
        session_options = {} 
        for index, center in enumerate(centers, 1):
            custom_id = center.get("centerId", "UnknownID")
            menu_text += f"{index}. {center['name']} ({center['location']})\n"
            session_options[str(index)] = str(custom_id)
        
        menu_text += "\nReply with the center number to book."

        await sms_sessions.update_one(
            {"phone": owner_phone}, 
            {"$set": {
                "vehicle_id": alert.vehicle_id,
                "user_id": user_id, 
                "issue": alert.issue_detected,
                "options": session_options,
                "state": "WAITING_FOR_CENTER", # <--- NEW: State tracking
                "timestamp": datetime.now()
            }},
            upsert=True 
        )

    # --- 5. SEND WHATSAPP ---
    try:
        message = twilio_client.messages.create(
            body=menu_text,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=f"whatsapp:{owner_phone}"
        )
        print(f"✅ ALERT SENT. SID: {message.sid}")
        return {"status": "success"}
    except Exception as e:
        print(f"❌ ERROR: Twilio failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sms-reply")
async def sms_reply(From: str = Form(...), Body: str = Form(...)):
    """
    Handles User Reply based on State -> Calls External Booking API -> Confirms to User
    """
    raw_phone = From
    user_phone = raw_phone.replace("whatsapp:", "").strip()
    user_choice = Body.strip()

    print(f"📩 REPLY from {user_phone}: '{user_choice}'")

    # 1. Retrieve Session
    session = await sms_sessions.find_one({"phone": user_phone})
    if not session:
        return PlainTextResponse("Session expired. Please wait for a new alert.")

    state = session.get("state", "WAITING_FOR_CENTER") # Fallback to manual if no state
    
    # Setup variables needed for API
    vehicle_id = session.get("vehicle_id")
    user_id = session.get("user_id", "UNKNOWN_USER")
    conf_code = f"CONF-{random.randint(100, 999)}"
    
    # We generate a dummy ISO date for the backend API so it doesn't crash on natural language
    tomorrow = datetime.utcnow() + timedelta(days=1)
    api_booking_time = tomorrow.replace(hour=10, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- IF STATE IS MANUAL (Waiting for Center Number) ---
    if state == "WAITING_FOR_CENTER":
        selected_center_id = session.get("options", {}).get(user_choice)
        if not selected_center_id:
            return PlainTextResponse("Invalid option. Please reply with the number (e.g., 1).")
        
        print(f"✅ USER SELECTED CENTER: {selected_center_id}")
        display_time = api_booking_time # Show dummy time for manual
    
    # --- IF STATE IS AUTO (Waiting for Date/Time) ---
    elif state == "WAITING_FOR_DATETIME":
        selected_center_id = session.get("auto_center_id")
        display_time = user_choice # Show their requested time in the WhatsApp response!
        print(f"✅ USER REQUESTED TIME: {display_time} AT CENTER: {selected_center_id}")

    # Construct the Payload for External API
    booking_payload = {
        "vehicleId": vehicle_id,
        "confirmationCode": conf_code,
        "status": "CONFIRMED",
        "scheduledService": {
            "isScheduled": True,
            "serviceCenterId": selected_center_id, 
            "dateTime": api_booking_time # Send ISO time to backend
        },
        "userId": user_id
    }

    print(f"🚀 CALLING EXTERNAL API: {BOOKING_API_URL}")

    # Call External API
    api_success = False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(BOOKING_API_URL, json=booking_payload)
            if response.status_code in [200, 201]:
                api_success = True
            else:
                print("❌ External API failed to book.")
    except Exception as e:
        print(f"❌ EXCEPTION Calling API: {e}")

    # Fetch Center Name for Display
    center = await admin_collection.find_one({"centerId": selected_center_id})
    center_name = center['name'] if center else "the service center"

    # Send Final Reply to User
    if api_success:
        response_msg = f"✅ BOOKING SUCCESSFUL!\n\nService scheduled at {center_name}\n📅 Time: {display_time}\n🔑 Code: {conf_code}\n\nDrive safely!"
    else:
        response_msg = f"⚠️ Booking initiated for {center_name} at {display_time}, but the system is slow. We will confirm shortly via email."

    # Optional: Clear the session so they don't double-book
    await sms_sessions.delete_one({"phone": user_phone})

    return PlainTextResponse(response_msg)