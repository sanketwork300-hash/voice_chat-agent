"""The list of documentation pages to ingest, and how to download them."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx
import yaml

DEFAULT_MANIFEST = Path("datasets/knowledge/manifest.yaml")
DEFAULT_CORPUS_DIR = Path("datasets/knowledge/corpus")


@dataclass(frozen=True)
class DocumentSpec:
    repo: str          # e.g. "awsdocs/amazon-ec2-user-guide"
    commit: str
    file: str          # file name inside doc_source/
    service: str
    category: str
    source: str
    source_date: date
    license: str

    @property
    def document_id(self) -> str:
        return f"{self.repo.split('/')[1]}/{Path(self.file).stem}"

    @property
    def source_url(self) -> str:
        """The exact text that was ingested, at its pinned commit."""
        return f"https://github.com/{self.repo}/blob/{self.commit}/doc_source/{self.file}"

    @property
    def raw_url(self) -> str:
        return f"https://raw.githubusercontent.com/{self.repo}/{self.commit}/doc_source/{self.file}"

    def local_path(self, corpus_dir: Path) -> Path:
        return corpus_dir / self.repo.split("/")[1] / self.commit[:8] / self.file


def load_manifest(path: Path = DEFAULT_MANIFEST) -> list[DocumentSpec]:
    data = yaml.safe_load(path.read_text())
    specs = [
        DocumentSpec(
            repo=repository["repo"], commit=repository["commit"], file=document["file"],
            service=repository["service"], category=document["category"], source=data["source"],
            source_date=repository["source_date"], license=repository["license"],
        )
        for repository in data["repositories"]
        for document in repository["documents"]
    ]
    ids = [spec.document_id for spec in specs]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"manifest lists the same page twice: {sorted(duplicates)}")
    return specs


def fetch(spec: DocumentSpec, corpus_dir: Path, client: httpx.Client, *, refetch: bool = False,
          offline: bool = False) -> tuple[str, bool]:
    """Return the page's Markdown and whether it was downloaded now (rather than read from the corpus directory)."""
    path = spec.local_path(corpus_dir)
    if path.exists() and not refetch:
        return path.read_text(), False
    if offline:
        raise FileNotFoundError(f"{path} is not in the corpus directory and --offline was given")
    response = client.get(spec.raw_url)
    response.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(response.text)
    return response.text, True
