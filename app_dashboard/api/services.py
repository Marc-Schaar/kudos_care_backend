from django.contrib.gis.geos import  Point
import requests
from ..models import Ride, RideStream

import polyline 
from shapely.geometry import LineString

class StravaImportService:
    @staticmethod
    def sync_activity_to_db(activity_data, access_token):
        """
        Wandelt Strava-JSON in ein Ride-Objekt um und speichert es in PostGIS.
        """
        polyline_str = activity_data.get('map', {}).get('summary_polyline')
        track = None

        if polyline_str:
            coords = polyline.decode(polyline_str)
            reduced_coords = GeoSimplifyService.reduce_track(coords, tolerance=0.001)
            track = LineString(reduced_coords, srid=4326)
            
        start_latlng = activity_data.get('start_latlng')
        point = None
        if start_latlng and len(start_latlng) == 2:
            point = Point(start_latlng[1], start_latlng[0], srid=4326)

        ride, created = Ride.objects.update_or_create(
            strava_id=activity_data['id'],
            defaults={
                'name': activity_data.get('name'),
                'track': track,
                'start_latlng': point
            }
        )

        stream_data = StravaStreamService.fetch_activity_streams(ride.strava_id, access_token)
        if stream_data:
            RideStream.objects.update_or_create(
                ride=ride,
                defaults={
                    'latlngs': stream_data.get('latlng', {}).get('data'),
                    'time_series': stream_data.get('time', {}).get('data')
                }
            )

        return ride
    

class StravaStreamService:
    @staticmethod
    def fetch_activity_streams(activity_id, access_token):
        url = f"https://www.strava.com/api/v3/activities/{activity_id}/streams"
        params = {
            'keys': 'latlng,time',
            'key_by_type': 'true'
        }
        headers = {'Authorization': f'Bearer {access_token}'}
        
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            return response.json()
        return None


class GeoSimplifyService:
    @staticmethod
    def reduce_track(points, tolerance=0.001):
        """
        :param points: Liste von (lon, lat) Tupeln
        :param tolerance: Grad der Vereinfachung (0.001 entspricht ca. 100-110m)
        """
        line = LineString(points)
        simplified = line.simplify(tolerance, preserve_topology=True)
        return list(simplified.coords)