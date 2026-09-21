class QaValidationRepository:
    """Read-only evidence from legacy QA; never a discovery source."""

    def load_day(self, connection, target_date) -> list[dict]:
        rows = connection.execute(
            "SELECT meeting_id, subject FROM public.qa_doctors_sessions WHERE date = %s",
            (target_date,),
        ).fetchall()
        return [{"meeting_id": row[0], "subject": row[1]} for row in rows]

