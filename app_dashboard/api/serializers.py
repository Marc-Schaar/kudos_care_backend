from rest_framework import serializers
from ..models import Ride
from app_auth.models import StravaProfile


class StravaAuthSerializer(serializers.Serializer):
    code = serializers.CharField(required=True, write_only=True)


class StravaSyncStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = StravaProfile
        fields = [
            "sync_status",
            "sync_started_at",
            "sync_finished_at",
            "sync_error",
            "last_sync_count",
        ]



class RideSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ride
        fields = '__all__'