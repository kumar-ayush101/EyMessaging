import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "auto_ai_db")

async def add_dummy_data():
    if not MONGO_URI:
        print("❌ Error: MONGO_URI is missing from your .env file")
        return

    print("⏳ Connecting to MongoDB...")
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]
    collection = db.service_centers

    # Data to Insert
    new_center = {
        "name": "Tata Motors Authorized Service",
        "company_name": "Tata",  
        "location": "Downtown, Main Street",
        "capacity": 50,
        "is_active": True,
        "bookings": []
    }

    try:
        result = await collection.insert_one(new_center)
        print(f"✅ SUCCESS! Added Service Center with ID: {result.inserted_id}")
    except Exception as e:
        print(f"❌ Failed to insert data: {e}")
    finally:
        client.close()

if __name__ == "__main__":
    asyncio.run(add_dummy_data())