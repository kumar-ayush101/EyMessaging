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
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
# Note: For WhatsApp Sandbox, this is usually +14155238886
# Check
TWILIO_WHATSAPP_NUMBER = "whatsapp:+14155238886" 

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

# 3. Connect to Database (HANDLING TWO DATABASES)
client = AsyncIOMotorClient(MONGO_URI)

# Database 1: Auto AI (For Service Centers & SMS Sessions)
db_auto = client["auto_ai_db"] 
admin_collection = db_auto.service_centers
sms_sessions = db_auto.sms_sessions

# Database 2: Techathon (For Vehicles & Users)
db_tech = client["techathon_db"]
vehicle_collection = db_tech.vehicles
users_collection = db_tech.users

# 5. Initialize Twilio Client
twilio_client = Client(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID else None

# --- MODELS ---
class SensorAlert(BaseModel):
    vehicle_id: str         # Format: "Maruti_V11" OR just "V11"
    issue_detected: str     

# --- API ENDPOINTS ---

@app.post("/sensor-anomaly")
async def sensor_anomaly_alert(alert: SensorAlert):
    if not twilio_client:
        print("❌ DEBUG: Twilio Client is None. Check env variables.")
        raise HTTPException(status_code=500, detail="Twilio not configured")

    # --- 1. FETCH OWNER PHONE FROM DB ---
    print(f"🔍 DEBUG: Looking up owner for vehicle: {alert.vehicle_id} in 'techathon_db'")
    
    # A. Get User ID from Vehicle
    vehicle = await vehicle_collection.find_one({"vehicle_id": alert.vehicle_id.strip()})
    
    if not vehicle:
        print(f"❌ DEBUG: Vehicle '{alert.vehicle_id}' not found in techathon_db.")
        return {"status": "error", "message": "Vehicle ID not registered."}
    
    user_id = vehicle.get("user_id")
    db_company_name = vehicle.get("model") 
    
    print(f"✅ DEBUG: Found Vehicle. Owner: {user_id}, Model: {db_company_name}")

    # B. Get Phone from User
    user = await users_collection.find_one({"user_id": user_id})
    if not user:
        print(f"❌ DEBUG: User {user_id} not found.")
        return {"status": "error", "message": "Vehicle owner not found."}

    # C. Set the variable
    owner_phone = user.get("phone")
    if not owner_phone:
        return {"status": "error", "message": "User has no phone number."}

    # --- 2. DETERMINE COMPANY NAME ---
    company_name = None
    if "_" in alert.vehicle_id:
        company_name = alert.vehicle_id.split("_")[0]
    elif db_company_name:
        company_name = db_company_name
    else:
        return {"status": "error", "message": "Could not determine company name."}

    # --- 3. FIND SERVICE CENTERS ---
    print(f"🔍 DEBUG: Searching centers for: '{company_name}'")
    
    centers_cursor = admin_collection.find({
        "company_name": {"$regex": f"^{company_name}", "$options": "i"}
    })
    
    # Fetch up to 5 centers
    centers = await centers_cursor.to_list(length=5)

    if not centers:
        print(f"⚠️ DEBUG: No centers found for {company_name}")
        return {"status": "warning", "message": f"No service centers found for {company_name}"}

    # --- 4. BUILD MULTI-OPTION MESSAGE ---
    menu_text = f"🚨 *ALERT:* {alert.issue_detected} detected for {alert.vehicle_id}.\n\n"
    menu_text += f"Select a {company_name} Service Center:\n"
    
    session_options = {} 
    
    # Loop through ALL found centers
    for index, center in enumerate(centers, 1):
        # We assume every center has a 'centerId' field based on your screenshot
        custom_id = center.get("centerId", "UnknownID")
        
        menu_text += f"{index}. {center['name']} ({center['location']})\n"
        
        # STORE THE CUSTOM ID (e.g., "C431") INSTEAD OF OBJECT_ID
        session_options[str(index)] = str(custom_id)
    
    menu_text += "\nReply with the center number to book."

    # --- 5. SAVE SESSION ---
    await sms_sessions.update_one(
        {"phone": owner_phone}, 
        {"$set": {
            "vehicle_id": alert.vehicle_id,
            "issue": alert.issue_detected,
            "options": session_options,
            "timestamp": datetime.now()
        }},
        upsert=True 
    )

    # --- 6. SEND WHATSAPP ---
    to_whatsapp = f"whatsapp:{owner_phone}"
    
    print(f"\n--- 🐛 DEBUGGING WHATSAPP ---")
    print(f"TO: {to_whatsapp}")
    print(f"MSG: \n{menu_text}")
    print(f"------------------------\n")

    try:
        message = twilio_client.messages.create(
            body=menu_text,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=to_whatsapp
        )
        print(f"✅ SUCCESS: WhatsApp Sent. SID: {message.sid}")
        return {
            "status": "success", 
            "message": "WhatsApp sent to owner", 
            "sid": message.sid, 
            "centers_found": len(centers)
        }
    except Exception as e:
        print(f"❌ ERROR: Twilio failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to send WhatsApp: {str(e)}")


@app.post("/sms-reply")
async def sms_reply(From: str = Form(...), Body: str = Form(...)):
    """
    Webhook for Twilio to call when User replies
    """
    # 1. Clean the phone number (Remove 'whatsapp:' prefix)
    raw_phone = From
    user_phone = raw_phone.replace("whatsapp:", "").strip()
    user_choice = Body.strip()

    print(f"📩 REPLY RECEIVED from {user_phone}: '{user_choice}'")

    # 2. Find the active session
    session = await sms_sessions.find_one({"phone": user_phone})

    if not session:
        print(f"❌ DEBUG: No session found for {user_phone}")
        return PlainTextResponse("No active service request found. Please wait for a new alert.")

    # 3. Check if choice is valid
    # This will now return "C431" instead of the MongoDB ObjectId
    selected_center_id = session.get("options", {}).get(user_choice)

    if selected_center_id:
        print(f"✅ BOOKING CONFIRMED! User selected Center ID: {selected_center_id}")
        
        # 4. Fetch center details using the CUSTOM ID (centerId)
        # We query by "centerId", NOT "_id"
        center = await admin_collection.find_one({"centerId": selected_center_id})
        
        center_name = center['name'] if center else "the service center"

        # 5. Success Response
        response_msg = f"✅ Confirmed! Booking request received for {center_name} (ID: {selected_center_id}). We are processing it now."
        return PlainTextResponse(response_msg)
    
    else:
        # Generic error message
        return PlainTextResponse("Invalid option. Please reply with the number corresponding to your choice.")