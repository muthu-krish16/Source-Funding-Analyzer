"""
Quick Gemini API health check.
Run:  python test_gemini.py
Tells you if your API key + model actually work, with the real error if not.
"""
import os
import google.generativeai as genai

KEY = os.environ.get("GEMINI_API_KEY", "").strip()
if not KEY:
    KEY = input("Enter your Gemini API key: ").strip()
genai.configure(api_key=KEY)

print("Key (masked):", (KEY[:8] + "..." + KEY[-4:]) if len(KEY) > 12 else "(short/empty)")
print("-" * 50)

# 1. List models your key can actually access
print("Models available to this key:")
try:
    found = False
    for m in genai.list_models():
        if "generateContent" in getattr(m, "supported_generation_methods", []):
            print("  -", m.name)
            found = True
    if not found:
        print("  (none returned — key likely invalid or no access)")
except Exception as e:
    print("  ERROR listing models:", type(e).__name__, e)

print("-" * 50)

# 2. Try a real call
MODEL = "gemini-3.5-flash"
print(f"Test call to {MODEL}...")
try:
    model = genai.GenerativeModel(MODEL)
    r = model.generate_content("Reply with the single word: OK")
    print("  Response:", repr(r.text))
    print("  >> SUCCESS — key and model work.")
except Exception as e:
    print("  ERROR:", type(e).__name__, e)
    print("  >> This is the real reason your app returns empty output.")
