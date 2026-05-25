from django.contrib.gis.geos import LineString
import polyline 

def decode_strava_polyline(polyline_str):
    coords = polyline.decode(polyline_str)
    points = [(lon, lat) for lat, lon in coords]
    return LineString(points, srid=4326)