from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Source, SourceKind


class SourceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_file(
        self,
        project_id: str,
        source_id: str,
        display_name: str,
        media_type: str,
        size_bytes: int,
        storage_path: str,
    ) -> Source:
        source = Source(
            id=source_id,
            project_id=project_id,
            kind=SourceKind.FILE,
            display_name=display_name,
            media_type=media_type,
            size_bytes=size_bytes,
            storage_path=storage_path,
            status="stored",
        )
        self._session.add(source)
        self._session.flush()
        return source

    def create_confluence(self, project_id: str, source_id: str, url: str) -> Source:
        source = Source(
            id=source_id,
            project_id=project_id,
            kind=SourceKind.CONFLUENCE,
            display_name=url,
            url=url,
            status="linked",
        )
        self._session.add(source)
        self._session.flush()
        return source

    def list_for_project(self, project_id: str) -> list[Source]:
        statement = select(Source).where(Source.project_id == project_id).order_by(
            Source.created_at, Source.id
        )
        return list(self._session.scalars(statement))

    def get(self, source_id: str) -> Source | None:
        return self._session.get(Source, source_id)

    def delete(self, source_id: str) -> bool:
        source = self.get(source_id)
        if source is None:
            return False
        self._session.delete(source)
        self._session.flush()
        return True
