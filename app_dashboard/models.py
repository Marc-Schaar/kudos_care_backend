from django.contrib.gis.db import models

class Ride(models.Model):
    strava_id = models.BigIntegerField(unique=True)
    name = models.CharField(max_length=255)
    track = models.LineStringField(srid=4326, null=True, blank=True) 
    start_latlng = models.PointField(srid=4326, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name