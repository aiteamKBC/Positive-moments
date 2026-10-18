"""Read-only Social Media API. GET only; nothing here writes."""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import social_clip_requests
from .models import DoctorSession
from .pagination import LecturePagination
from .social_serializers import SocialLectureDetailSerializer, SocialLectureSerializer
from .social_services import (
    InvalidFilter,
    accepted_opportunities,
    opportunity_stats,
    social_lectures,
    social_summary,
)
from .utils import decode_session_id


def _invalid(exc: InvalidFilter):
    return Response({"code": exc.code, "detail": exc.detail}, status=status.HTTP_400_BAD_REQUEST)


def _not_found():
    return Response({"code": "not_found",
                     "detail": "This lecture has no accepted social media opportunities."},
                    status=status.HTTP_404_NOT_FOUND)


class SocialSummaryView(APIView):
    def get(self, request):
        try:
            return Response(social_summary(request.query_params))
        except InvalidFilter as exc:
            return _invalid(exc)


class SocialLectureListView(APIView):
    def get(self, request):
        try:
            queryset = social_lectures(request.query_params)
        except InvalidFilter as exc:
            return _invalid(exc)
        paginator = LecturePagination()
        page = paginator.paginate_queryset(queryset, request)
        stats = opportunity_stats(lecture.session_id for lecture in page)
        data = SocialLectureSerializer(page, many=True, context={"stats": stats}).data
        return paginator.get_paginated_response(data)


class SocialLectureDetailView(APIView):
    def get(self, request, session_key):
        try:
            session_id = decode_session_id(session_key)
        except ValueError:
            return _not_found()
        lecture = DoctorSession.objects.defer("positive_clips").filter(session_id=session_id).first()
        if lecture is None:
            return _not_found()
        opportunities = list(
            accepted_opportunities().filter(session_id=session_id)
            .order_by("start_cue", "end_cue", "opportunity_id")
        )
        if not opportunities:
            return _not_found()
        context = {"stats": opportunity_stats([session_id]), "opportunities": opportunities}
        return Response(SocialLectureDetailSerializer(lecture, context=context).data)


class SocialClipRequestView(APIView):
    """
    Create Clip on one reel. Validates here, then forwards
    {session_id, opportunity_id} to the clip-on-demand webhook. 202 means the
    request was accepted, not that the clip exists; the clip is ready only
    once n8n has saved a SharePoint link.
    """

    def post(self, request, session_key, opportunity_id):
        try:
            session_id = decode_session_id(session_key)
        except ValueError:
            return _not_found()
        try:
            clip = social_clip_requests.request_clip(session_id, opportunity_id, request.user.get_username())
        except social_clip_requests.ClipRefused as exc:
            body = {"code": exc.code, "detail": exc.detail}
            if exc.clip is not None:
                body["clip"] = exc.clip
            return Response(body, status=exc.status)
        return Response({"status": "requested", "clip": clip}, status=status.HTTP_202_ACCEPTED)


class SocialClipStatusView(APIView):
    """Every reel's clip state in one lecture, read from PostgreSQL. What the page polls."""

    def get(self, request, session_key):
        try:
            session_id = decode_session_id(session_key)
        except ValueError:
            return _not_found()
        lecture = DoctorSession.objects.defer("positive_clips").filter(session_id=session_id).first()
        opportunities = list(accepted_opportunities().filter(session_id=session_id)) if lecture else []
        if not opportunities:
            return _not_found()
        clips = {}
        for opportunity in opportunities:
            state = social_clip_requests.clip_state(opportunity, lecture)
            if state is not None:
                clips[str(opportunity.opportunity_id)] = state
        return Response({"clips": clips})
