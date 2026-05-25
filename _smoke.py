import sys, os
print("python", sys.version.split()[0])
print("cwd", os.getcwd())
try:
    import faiss; print("faiss OK")
except Exception as e:
    print("faiss FAIL:", e)
try:
    import torch; print("torch", torch.__version__)
except Exception as e:
    print("torch FAIL:", e)
try:
    import sentence_transformers as st; print("sentence_transformers OK", st.__version__)
except Exception as e:
    print("st FAIL:", e)
try:
    import paddleocr; print("paddleocr OK", getattr(paddleocr, "__version__", "?"))
except Exception as e:
    print("paddleocr FAIL:", e)
try:
    import paddle; print("paddle", paddle.__version__)
except Exception as e:
    print("paddle FAIL:", e)
try:
    import fitz; print("pymupdf OK", fitz.__doc__.splitlines()[0] if fitz.__doc__ else "ok")
except Exception as e:
    print("pymupdf FAIL:", e)
