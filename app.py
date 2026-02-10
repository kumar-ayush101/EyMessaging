from fastapi import FastAPI, HTTPException, Body, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.responses import PlainTextResponse
from motor.motor_asyncio import AsyncIOMotorClient
from twilio.rest import Client
import os
from datetime import datetime
from dotenv import load_dotenv
from bson import ObjectId

load_dotenv()

# --- CONFIGURATION ---
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "auto_ai_db")
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE = os.getenv("TWILIO_PHONE_NUMBER")

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
db = client[DB_NAME]

# 4. Define Collections
admin_collection = db.service_centers
sms_sessions = db.sms_sessions 

# 5. Initialize Twilio Client
twilio_client = Client(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID else None

# --- MODELS ---
class SensorAlert(BaseModel):
    vehicle_id: str         # Expected format: "Maruti_Swift_V11"
    issue_detected: str     
    owner_phone: str        

# --- API ENDPOINTS ---

@app.post("/sensor-anomaly")
async def sensor_anomaly_alert(alert: SensorAlert):
    if not twilio_client:
        print("❌ DEBUG: Twilio not configured properly.")
        raise HTTPException(status_code=500, detail="Twilio not configured")

    # --- 1. SMART VEHICLE ID PARSING ---
    # We check if there is an underscore to separate Company_Model
    if "_" in alert.vehicle_id:
        company_name = alert.vehicle_id.split("_")[0]
    else:
        # If user sends "V11", we stop here and tell them the format is wrong
        print(f"⚠️ DEBUG: Invalid ID format received: {alert.vehicle_id}")
        return {
            "status": "error", 
            "message": "Invalid Vehicle ID. Please use format 'Company_Model' (e.g., 'Maruti_V11')."
        }

    # --- 2. FIND SERVICE CENTERS ---
    print(f"🔍 DEBUG: Searching for centers matching company: '{company_name}'")
    
    centers_cursor = admin_collection.find({
        "company_name": {"$regex": f"^{company_name}", "$options": "i"}
    })
    centers = await centers_cursor.to_list(length=5)

    if not centers:
        print(f"⚠️ DEBUG: No service centers found in DB for '{company_name}'")
        return {
            "status": "warning", 
            "message": f"No centers found for company '{company_name}'. Please register a center with company_name='{company_name}'."
        }

    # --- 3. BUILD SMS & SESSION ---
    menu_text = f"🚨 ALERT: {alert.issue_detected} detected for {alert.vehicle_id}.\n\nReply with a number to book:\n"
    session_options = {} 
    
    for index, center in enumerate(centers, 1):
        menu_text += f"{index}. {center['name']} ({center['location']})\n"
        session_options[str(index)] = str(center["_id"]) 
    
    # Save session to DB
    await sms_sessions.update_one(
        {"phone": alert.owner_phone}, 
        {"$set": {
            "vehicle_id": alert.vehicle_id,
            "issue": alert.issue_detected,
            "options": session_options,
            "timestamp": datetime.now()
        }},
        upsert=True 
    )

    # --- 4. SEND SMS ---
    print(f"📤 DEBUG: Sending SMS to {alert.owner_phone}...")
    try:
        message = twilio_client.messages.create(
            body=menu_text,
            from_=TWILIO_PHONE,
            to=alert.owner_phone
        )
        print(f"✅ SUCCESS: SMS Sent. SID: {message.sid}")
        return {"status": "success", "centers_found": len(centers), "sid": message.sid}
    except Exception as e:
        print(f"❌ ERROR: Twilio failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sms-reply")
async def sms_reply(From: str = Form(...), Body: str = Form(...)):
    """
    Webhook for Twilio to call when User replies
    """
    user_phone = From
    user_choice = Body.strip()

    print(f"📩 SMS RECEIVED from {user_phone}: '{user_choice}'")

    # 1. Find the active session
    session = await sms_sessions.find_one({"phone": user_phone})

    if not session:
        return PlainTextResponse("No active service request found. Please contact support.")

    # 2. Check if choice is valid (e.g., "1")
    selected_center_id = session.get("options", {}).get(user_choice)

    if selected_center_id:
        print(f"✅ USER SELECTED CENTER ID: {selected_center_id}")
        
        # 3. Fetch Center Name for nice reply
        center = await admin_collection.find_one({"_id": ObjectId(selected_center_id)})
        center_name = center['name'] if center else "the service center"

        # 4. Reply to User
        response_msg = f"✅ Confirmed! Appointment request sent to {center_name}. Center ID: {selected_center_id}"
        return PlainTextResponse(response_msg)
    
    else:
        return PlainTextResponse("Invalid option. Please reply with the number corresponding to your choice (e.g., 1).")