import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.ensemble import RandomForestClassifier
import joblib
import random

# 1. Prepare Synthetic Data (Logic: Length & Complexity Words -> Action)
# Categories:
# - Short/Simple -> "Expand/Creative" (Not implemented yet, but good for training)
# - Long/Complex -> "Summarize"
# - Emotional/Opinionated -> "Sentiment"
# Note: For this demo, we will train it to detect "Academic/Complex" vs "Casual/Simple"
# which dictates the TONE of the answer.

data = [
    ("The quantum mechanics of this phenomenon are intricate.", "academic"),
    ("I love this pizza, it is tasty!", "casual"),
    ("The geopolitical situation requires detailed analysis.", "academic"),
    ("Hey what's up? doing good?", "casual"),
    ("The statistical deviation is significant.", "academic"),
    ("OMG this is so cool", "casual"),
    ("Furthermore, the evidence suggests a correlation.", "academic"),
    ("No way, that's crazy", "casual"),
    ("The algorithm complexity is O(n log n).", "academic"),
    ("Just chilling at home.", "casual"),
    ("The dataset exhibits a normal distribution.", "academic"),
    ("brb, gotta grab some food", "casual"),
    ("idk what you mean tbh", "casual"),
    ("The implications of this study are profound.", "academic"),
    ("lol that was so funny", "casual"),
    ("sup, how have you been?", "casual"),
    ("The quantitative analysis reveals a distinct trend.", "academic"),
    ("imonna go to the store", "casual"),
    ("yo bro, can u send me the files ASAP? i tried textin u but u dint reply. thx.", "casual"),
    # Code Examples
    ("print('hello world')", "code"),
    ("def calculate_sum(a, b): return a + b", "code"),
    ("import numpy as np", "code"),
    ("const x = 5;", "code"),
    ("for i in range(10): print(i)", "code"),
    ("Error: undefined variable", "code"),
    # Angry Examples
    ("This is stupid and I hate it.", "angry"),
    ("You are an idiot, this service sucks!", "angry"),
    ("I am extremely furious about this issue.", "angry"),
    ("Don't ever talk to me again!", "angry"),
    ("This is a complete waste of time.", "angry"),
]

# Expand data slightly for better "fake" training
more_academic = [
    "analysis", "study", "research", "methodology", "hypothesis", "significant", "data", "theory", "phenomenon", "structure",
    "evaluation", "context", "framework", "perspective", "implementation", "variable", "correlation", "deduction"
]
more_casual = [
    "cool", "nice", "fun", "lol", "yeah", "nah", "super", "really", "love", "hate",
    "bro", "dude", "tbh", "idk", "asap", "plz", "thx", "omg", "gotta", "gonna"
]
more_code = [
    "print", "return", "def", "function", "var", "const", "class", "import", "if", "else", "for", "while"
]
more_angry = [
    "stupid", "idiot", "sucks", "hate", "furious", "worst", "terrible", "garbage", "useless", "angry"
]

for _ in range(100):
    # Generate academic-ish sentences
    s = f"The {random.choice(more_academic)} of the {random.choice(more_academic)} is {random.choice(more_academic)}."
    data.append((s, "academic"))
    
    # Generate casual sentences - DIVERSE PATTERNS
    # Pattern 1: "This is..."
    s1 = f"This is {random.choice(more_casual)} and {random.choice(more_casual)} {random.choice(more_casual)}."
    data.append((s1, "casual"))
    # Pattern 2: Short 2-word greetings/statements
    s2 = f"{random.choice(more_casual)} {random.choice(more_casual)}"
    data.append((s2, "casual"))
    # Pattern 3: Just one word (detected as casual)
    s3 = f"{random.choice(more_casual)}"
    data.append((s3, "casual"))

    # Generate code snippets
    # Pattern 1: Assignment
    s_code1 = f"{random.choice(more_code)} x = {random.choice(more_code)}()"
    data.append((s_code1, "code"))
    # Pattern 2: Function call
    s_code2 = f"{random.choice(more_code)}({random.choice(more_code)})"
    data.append((s_code2, "code"))

    # Generate angry sentences
    s = f"This is {random.choice(more_angry)} and {random.choice(more_angry)}!"
    data.append((s, "angry"))

texts, labels = zip(*data)

# 2. Vectorize
print("Vectorizing text...")
vectorizer = TfidfVectorizer()
X = vectorizer.fit_transform(texts)

# 3. Train Model
print("Training Random Forest...")
clf = RandomForestClassifier(n_estimators=10)
clf.fit(X, labels)

# 4. Save
print("Saving model and vectorizer...")
joblib.dump(clf, 'model.pkl')
joblib.dump(vectorizer, 'vectorizer.pkl')

print("Done! Decision AI is ready.")
