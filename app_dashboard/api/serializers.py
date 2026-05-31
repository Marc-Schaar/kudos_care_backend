from rest_framework import serializers
from ..models import Ride


class StravaAuthSerializer(serializers.Serializer):
    code = serializers.CharField(required=True, write_only=True)




class RideSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ride
        fields = '__all__'