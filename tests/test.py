"""Optional local model smoke script; it is not executed by pytest."""

from pathlib import Path


def run() -> None:
    """Manually verify the local model from the project root."""
    from sentence_transformers import SentenceTransformer
    from sklearn.metrics.pairwise import cosine_similarity

    project_root = Path(__file__).resolve().parents[1]
    model = SentenceTransformer(
        str(project_root / "models" / "finetuned-bert"), local_files_only=True
    )
    resume_embedding = model.encode(
        ["Python developer with experience in machine learning and data analysis."]
    )
    job_description_embedding = model.encode(
        ["Looking for a Python developer skilled in machine learning."]
    )
    similarity = cosine_similarity(resume_embedding, job_description_embedding)[0][0]
    print("Model loaded successfully!")
    print("Semantic Similarity:", round(float(similarity), 4))


if __name__ == "__main__":
    run()
