
import os
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

uri = os.getenv("MONGO_URI")

if not uri:
    raise ValueError("MONGO_URI not found in .env")

client = MongoClient(
    uri,
    serverSelectionTimeoutMS=10000
)

try:
    result = client.admin.command("ping")
    print("Atlas connection successful!")
    print(result)
except Exception as e:
    print("Connection failed:", e)
finally:
    client.close()
