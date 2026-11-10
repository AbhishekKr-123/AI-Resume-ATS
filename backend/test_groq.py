
import os
from dotenv import load_dotenv
from groq import Groq

# Load environment variables from project root
load_dotenv()

api_key = os.getenv("GROQ_API_KEY")
model_name = os.getenv("GROQ_MODEL")

if not api_key:
    raise ValueError("GROQ_API_KEY is missing from .env")

if not model_name:
    raise ValueError("GROQ_MODEL is missing from .env")

# Initialize Groq client
client = Groq(api_key=api_key)

try:
    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {
                "role": "user",
                "content": "Explain what an ATS resume score means in two sentences."
            }
        ]
    )

    print("\nGroq API connected successfully!")
    print("Model:", model_name)
    print("\nAI Response:")
    print(response.choices[0].message.content)

except Exception as e:
    print("\nGroq API test failed!")
    print("Error:", str(e))
