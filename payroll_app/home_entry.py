"""The empty address (/): a visitor who is not logged in sees the public home page; a logged-in person gets the dashboard exactly as before."""
from . import views


def home_entry(request):
    if request.user.is_authenticated:
        return views.dashboard(request)         # the dashboard view itself still sends admins and employees to their own pages
    return views.landing(request)
