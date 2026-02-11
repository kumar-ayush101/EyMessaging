from fastapi import FastAPI, HTTPException, Body, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.responses import PlainTextResponse
from motor.motor_asyncio import AsyncIOMotorClient
from twilio.rest import Client
import os
import httpx  # <--- NEW: For calling external APIs
import random # <--- NEW: For generating dummy confirmation codes
from datetime import datetime, timedelta
from dotenv import load_dotenv
from bson import ObjectId

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

# --- API ENDPOINTS ---

@app.post("/sensor-anomaly")
async def sensor_anomaly_alert(alert: SensorAlert):
    if not twilio_client:
        raise HTTPException(status_code=500, detail="Twilio not configured")

    # --- 1. FETCH DETAILS ---
    print(f"🔍 DEBUG: Looking up owner for: {alert.vehicle_id}")
    
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

    # --- 3. FIND CENTERS ---
    centers_cursor = admin_collection.find({
        "company_name": {"$regex": f"^{company_name}", "$options": "i"}
    })
    centers = await centers_cursor.to_list(length=5)

    if not centers:
        return {"status": "warning", "message": f"No service centers found for {company_name}"}

    # --- 4. BUILD MESSAGE & SAVE SESSION ---
    menu_text = f"🚨 *ALERT:* {alert.issue_detected} detected for {alert.vehicle_id}.\n\n"
    menu_text += f"Select a {company_name} Service Center:\n"
    
    session_options = {} 
    
    for index, center in enumerate(centers, 1):
        custom_id = center.get("centerId", "UnknownID")
        menu_text += f"{index}. {center['name']} ({center['location']})\n"
        session_options[str(index)] = str(custom_id)
    
    menu_text += "\nReply with the center number to book."

    # ✅ UPDATED: Saving 'user_id' in session so we can use it for booking later
    await sms_sessions.update_one(
        {"phone": owner_phone}, 
        {"$set": {
            "vehicle_id": alert.vehicle_id,
            "user_id": user_id,  # <--- CRITICAL NEW FIELD
            "issue": alert.issue_detected,
            "options": session_options,
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
        return {"status": "success", "centers_found": len(centers)}
    except Exception as e:
        print(f"❌ ERROR: Twilio failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sms-reply")
async def sms_reply(From: str = Form(...), Body: str = Form(...)):
    """
    Handles User Reply -> Calls External Booking API -> Confirms to User
    """
    raw_phone = From
    user_phone = raw_phone.replace("whatsapp:", "").strip()
    user_choice = Body.strip()

    print(f"📩 REPLY from {user_phone}: '{user_choice}'")

    # 1. Retrieve Session
    session = await sms_sessions.find_one({"phone": user_phone})
    if not session:
        return PlainTextResponse("Session expired. Please wait for a new alert.")

    selected_center_id = session.get("options", {}).get(user_choice)

    if selected_center_id:
        print(f"✅ USER SELECTED CENTER: {selected_center_id}")
        
        # 2. Prepare Data for External API
        vehicle_id = session.get("vehicle_id")
        user_id = session.get("user_id", "UNKNOWN_USER")
        
        # Generate a dummy future date (e.g., Tomorrow at 10 AM)
        tomorrow = datetime.utcnow() + timedelta(days=1)
        booking_time = tomorrow.replace(hour=10, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # Generate a dummy confirmation code
        conf_code = f"CONF-{random.randint(100, 999)}"

        # Construct the Payload
        booking_payload = {
            "vehicleId": vehicle_id,
            "confirmationCode": conf_code,
            "status": "CONFIRMED",
            "scheduledService": {
                "isScheduled": True,
                "serviceCenterId": selected_center_id, # The ID user selected (e.g., C431)
                "dateTime": booking_time
            },
            "userId": user_id
        }

        print(f"🚀 CALLING EXTERNAL API: {BOOKING_API_URL}")
        print(f"📦 PAYLOAD: {booking_payload}")

        # 3. Call External API
        api_success = False
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(BOOKING_API_URL, json=booking_payload)
                
                print(f"📡 API RESPONSE CODE: {response.status_code}")
                print(f"📡 API RESPONSE BODY: {response.text}")
                
                if response.status_code in [200, 201]:
                    api_success = True
                else:
                    print("❌ External API failed to book.")

        except Exception as e:
            print(f"❌ EXCEPTION Calling API: {e}")

        # 4. Fetch Center Name for Display
        center = await admin_collection.find_one({"centerId": selected_center_id})
        center_name = center['name'] if center else "the service center"

        # 5. Send Final Reply to User
        if api_success:
            response_msg = f"✅ BOOKING SUCCESSFUL!\n\nService scheduled at {center_name} (ID: {selected_center_id})\n📅 Date: {booking_time}\n🔑 Code: {conf_code}\n\nDrive safely!"
        else:
            response_msg = f"⚠️ Booking initiated for {center_name}, but the external system is slow. We will confirm shortly via email."

        return PlainTextResponse(response_msg)
    
    else:
        return PlainTextResponse("Invalid option. Please reply with the number (e.g., 1).")