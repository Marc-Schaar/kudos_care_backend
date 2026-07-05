from rest_framework import serializers


class StravaAuthSerializer(serializers.Serializer):
    code = serializers.CharField(required=True, write_only=True)
    scope = serializers.CharField(required=False, allow_blank=True, write_only=True)
