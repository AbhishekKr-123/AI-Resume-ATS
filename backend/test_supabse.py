
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_PUBLISHABLE_KEY")

if not url or not key:
    raise ValueError("Supabase credentials missing from .env")

try:
    response = httpx.get(
        f"{url}/rest/v1/",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}"
        },
        timeout=15
    )

    if response.status_code == 200:
        print("Supabase connection successful!")
    else:
        print("Supabase responded with:", response.status_code)
        print(response.text[:500])

except Exception as e:
    print("Supabase connection failed:", str(e))
