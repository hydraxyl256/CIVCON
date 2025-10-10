import nltk

try:
    nltk.data.find("tokenizers/punkt")
    print("Punkt tokenizer already installed.")
except LookupError:
    print("Downloading Punkt tokenizer...")
    nltk.download("punkt")
    print("Download complete.")
