from rest_framework import serializers


class StravaAuthSerializer(serializers.Serializer):
    code = serializers.CharField(required=True, write_only=True)
