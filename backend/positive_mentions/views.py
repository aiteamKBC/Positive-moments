from django.contrib.auth import authenticate
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import DoctorSession
from .pagination import LecturePagination
from .serializers import LectureDetailSerializer, LectureListSerializer
from .services import (
    filtered_lectures,
    v5_lectures,
    with_clip_production_status,
    with_positive_clips_length,
)
from .utils import decode_session_id, normalize_clips, timestamp_to_seconds, timestamped_sharepoint_url

WATCH_PREROLL_SECONDS = 60


@api_view(["POST"])
@permission_classes([AllowAny])
def login_view(request):
    username = request.data.get("username", "")
    password = request.data.get("password", "")
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        return Response(
            {"code": "invalid_credentials", "detail": "Invalid username or password."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    token, _ = Token.objects.get_or_create(user=user)
    return Response({
        "token": token.key,
        "user": {"username": user.get_username(), "is_staff": user.is_staff},
    })


@api_view(["POST"])
def logout_view(request):
    if request.auth:
        request.auth.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET"])
def me_view(request):
    return Response({
        "username": request.user.get_username(),
        "is_staff": request.user.is_staff,
    })


class SummaryView(APIView):
    def get(self, request):
        queryset = with_positive_clips_length(filtered_lectures(request.query_params))
        available = queryset.exclude(recording_url__isnull=True).exclude(recording_url="").count()
        totals = queryset.aggregate(
            processed=Count("session_id"),
            with_clips=Count("session_id", filter=Q(positive_clips_length__gt=0)),
            total_clips=Coalesce(Sum("positive_clips_length"), 0),
        )
        processed = totals["processed"]
        with_clips = totals["with_clips"]
        return Response({
            "processed_lectures": processed,
            "lectures_with_positive_clips": with_clips,
            "lectures_without_positive_clips": processed - with_clips,
            "total_positive_clips": totals["total_clips"],
            "recordings_available": available,
            "recordings_missing": processed - available,
        })


class LectureListView(APIView):
    def get(self, request):
        queryset = filtered_lectures(request.query_params)
        paginator = LecturePagination()
        page = paginator.paginate_queryset(queryset, request)
        data = LectureListSerializer(page, many=True).data
        return paginator.get_paginated_response(data)


def lecture_from_key(session_key):
    try:
        session_id = decode_session_id(session_key)
    except ValueError:
        return None
    return get_object_or_404(
        with_clip_production_status(v5_lectures()),
        session_id=session_id,
    )


class LectureDetailView(APIView):
    def get(self, request, session_key):
        lecture = lecture_from_key(session_key)
        if lecture is None:
            return Response(
                {"code": "invalid_session_id", "detail": "The lecture identifier is invalid."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(LectureDetailSerializer(lecture).data)


class WatchMomentView(APIView):
    def get(self, request, session_key, clip_index):
        lecture = lecture_from_key(session_key)
        if lecture is None:
            return Response(
                {"code": "invalid_session_id", "detail": "The lecture identifier is invalid."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not lecture.recording_url:
            return Response({
                "code": "recording_not_available",
                "detail": "The recording link has not been stored yet.",
            }, status=status.HTTP_409_CONFLICT)
        clips = normalize_clips(lecture.positive_clips)
        if clip_index < 0 or clip_index >= len(clips):
            return Response({
                "code": "invalid_clip_index",
                "detail": "The requested positive moment does not exist.",
            }, status=status.HTTP_404_NOT_FOUND)
        seconds = timestamp_to_seconds(clips[clip_index].get("start"))
        if seconds is None:
            return Response({
                "code": "invalid_clip_timestamp",
                "detail": "This positive moment does not have a valid start timestamp.",
            }, status=status.HTTP_422_UNPROCESSABLE_CONTENT)
        playback_seconds = max(0, seconds - WATCH_PREROLL_SECONDS)
        return Response({
            "url": timestamped_sharepoint_url(lecture.recording_url, playback_seconds)
        })
