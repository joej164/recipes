from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.core.cache import caches
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.http import HttpResponseRedirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext as _
from rest_framework import permissions
from rest_framework.permissions import SAFE_METHODS

from cookbook.models import ShareLink, Recipe, UserPreference, UserSpace


def get_allowed_groups(groups_required):
    """
    Builds a list of all groups equal or higher to the provided groups
    This means checking for guest will also allow admins to access
    :param groups_required: list or tuple of groups
    :return: tuple of groups
    """
    groups_allowed = tuple(groups_required)
    if 'guest' in groups_required:
        groups_allowed = groups_allowed + ('user', 'admin')
    if 'user' in groups_required:
        groups_allowed = groups_allowed + ('admin',)
    return groups_allowed


def has_group_permission(user, groups):
    """
    Tests if a given user is member of a certain group (or any higher group)
    Superusers always bypass permission checks.
    Unauthenticated users can't be member of any group thus always return false.
    :param user: django auth user object
    :param groups: list or tuple of groups the user should be checked for
    :return: True if user is in allowed groups, false otherwise
    """
    if not user.is_authenticated:
        return False
    groups_allowed = get_allowed_groups(groups)
    if user.is_authenticated:
        if user_space := user.userspace_set.filter(active=True):
            if len(user_space) != 1:
                return False  # do not allow any group permission if more than one space is active, needs to be changed when simultaneous multi-space-tenancy is added
            if bool(user_space.first().groups.filter(name__in=groups_allowed)):
                return True
    return False


def is_object_owner(user, obj):
    """
    Tests if a given user is the owner of a given object
    test performed by checking user against the objects user
    and create_by field (if exists)
    :param user django auth user object
    :param obj any object that should be tested
    :return: true if user is owner of object, false otherwise
    """
    if not user.is_authenticated:
        return False
    try:
        return obj.get_owner() == user
    except Exception:
        return False


def is_space_owner(user, obj):
    """
    Tests if a given user is the owner the space of a given object
    :param user django auth user object
    :param obj any object that should be tested
    :return: true if user is owner of the objects space, false otherwise
    """
    if not user.is_authenticated:
        return False
    try:
        return obj.get_space().get_owner() == user
    except Exception:
        return False


def is_object_shared(user, obj):
    """
    Tests if a given user is shared for a given object
    test performed by checking user against the objects shared table
    :param user django auth user object
    :param obj any object that should be tested
    :return: true if user is shared for object, false otherwise
    """
    # TODO this could be improved/cleaned up by adding
    #      share checks for relevant objects
    if not user.is_authenticated:
        return False
    return user in obj.get_shared()


def share_link_valid(recipe, share):
    """
    Verifies the validity of a share uuid
    :param recipe: recipe object
    :param share: share uuid
    :return: true if a share link with the given recipe and uuid exists
    """
    try:
        CACHE_KEY = f'recipe_share_{recipe.pk}_{share}'
        if c := caches['default'].get(CACHE_KEY, False):
            return c

        if link := ShareLink.objects.filter(recipe=recipe, uuid=share, abuse_blocked=False).first():
            if 0 < settings.SHARING_LIMIT < link.request_count:
                return False
            link.request_count += 1
            link.save()
            caches['default'].set(CACHE_KEY, True, timeout=3)
            return True
        return False
    except ValidationError:
        return False


# Django Views

def group_required(*groups_required):
    """
    Decorator that tests the requesting user to be member
    of at least one of the provided groups or higher level groups
    :param groups_required: list of required groups
    :return: true if member of group, false otherwise
    """

    def in_groups(u):
        return has_group_permission(u, groups_required)

    return user_passes_test(in_groups, login_url='view_no_perm')


class GroupRequiredMixin(object):
    """
        groups_required - list of strings, required param
    """

    groups_required = None

    def dispatch(self, request, *args, **kwargs):
        if not has_group_permission(request.user, self.groups_required):
            if not request.user.is_authenticated:
                messages.add_message(request, messages.ERROR,
                                     _('You are not logged in and therefore cannot view this page!'))
                return HttpResponseRedirect(reverse_lazy('account_login') + '?next=' + request.path)
            else:
                messages.add_message(request, messages.ERROR,
                                     _('You do not have the required permissions to view this page!'))
                return HttpResponseRedirect(reverse_lazy('index'))
        try:
            obj = self.get_object()
            if obj.get_space() != request.space:
                messages.add_message(request, messages.ERROR,
                                     _('You do not have the required permissions to view this page!'))
                return HttpResponseRedirect(reverse_lazy('index'))
        except AttributeError:
            pass

        return super(GroupRequiredMixin, self).dispatch(request, *args, **kwargs)


class OwnerRequiredMixin(object):

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.add_message(request, messages.ERROR,
                                 _('You are not logged in and therefore cannot view this page!'))
            return HttpResponseRedirect(reverse_lazy('account_login') + '?next=' + request.path)
        else:
            if not is_object_owner(request.user, self.get_object()):
                messages.add_message(request, messages.ERROR,
                                     _('You cannot interact with this object as it is not owned by you!'))
                return HttpResponseRedirect(reverse('index'))

        try:
            obj = self.get_object()
            if not request.user.userspace.filter(space=obj.get_space()).exists():
                messages.add_message(request, messages.ERROR,
                                     _('You do not have the required permissions to view this page!'))
                return HttpResponseRedirect(reverse_lazy('index'))
        except AttributeError:
            pass

        return super(OwnerRequiredMixin, self).dispatch(request, *args, **kwargs)


# Django Rest Framework Permission classes

class CustomIsOwner(permissions.BasePermission):
    """
    Custom permission class for django rest framework views
    verifies user has ownership over object
    (either user or created_by or user is request user)
    """
    message = _('You cannot interact with this object as it is not owned by you!')

    def has_permission(self, request, view):
        return request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        return is_object_owner(request.user, obj)


class CustomIsOwnerReadOnly(CustomIsOwner):
    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.method in SAFE_METHODS

    def has_object_permission(self, request, view, obj):
        return super().has_object_permission(request, view) and request.method in SAFE_METHODS


class CustomIsSpaceOwner(permissions.BasePermission):
    """
    Custom permission class for django rest framework views
    verifies if the user is the owner of the space the object belongs to
    """
    message = _('You cannot interact with this object as it is not owned by you!')

    def has_permission(self, request, view):
        return request.user.is_authenticated and request.space.created_by == request.user

    def has_object_permission(self, request, view, obj):
        return is_space_owner(request.user, obj)


# TODO function duplicate/too similar name
class CustomIsShared(permissions.BasePermission):
    """
    Custom permission class for django rest framework views
    verifies user is shared for the object he is trying to access
    """
    message = _('You cannot interact with this object as it is not owned by you!')  # noqa: E501

    def has_permission(self, request, view):
        return request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        # # temporary hack to make old shopping list work with new shopping list
        # if obj.__class__.__name__ in ['ShoppingList', 'ShoppingListEntry']:
        #     return is_object_shared(request.user, obj) or obj.created_by in list(request.user.get_shopping_share())
        return is_object_shared(request.user, obj)


class CustomIsGuest(permissions.BasePermission):
    """
    Custom permission class for django rest framework views
    verifies the user is member of at least the group: guest
    """
    message = _('You do not have the required permissions to view this page!')

    def has_permission(self, request, view):
        return has_group_permission(request.user, ['guest'])

    def has_object_permission(self, request, view, obj):
        return has_group_permission(request.user, ['guest'])


class CustomIsUser(permissions.BasePermission):
    """
    Custom permission class for django rest framework views
    verifies the user is member of at least the group: user
    """
    message = _('You do not have the required permissions to view this page!')

    def has_permission(self, request, view):
        return has_group_permission(request.user, ['user'])


class CustomIsAdmin(permissions.BasePermission):
    """
    Custom permission class for django rest framework views
    verifies the user is member of at least the group: admin
    """
    message = _('You do not have the required permissions to view this page!')

    def has_permission(self, request, view):
        return has_group_permission(request.user, ['admin'])


class CustomIsShare(permissions.BasePermission):
    """
    Custom permission class for django rest framework views
    verifies the requesting user provided a valid share link
    """
    message = _('You do not have the required permissions to view this page!')

    def has_permission(self, request, view):
        return request.method in SAFE_METHODS and 'pk' in view.kwargs

    def has_object_permission(self, request, view, obj):
        share = request.query_params.get('share', None)
        if share:
            return share_link_valid(obj, share)
        return False


def above_space_limit(space):  # TODO add file storage limit
    """
    Test if the space has reached any limit (e.g. max recipes, users, ..)
    :param space: Space to test for limits
    :return: Tuple (True if above or equal any limit else false, message)
    """
    r_limit, r_msg = above_space_recipe_limit(space)
    u_limit, u_msg = above_space_user_limit(space)
    return r_limit or u_limit, (r_msg + ' ' + u_msg).strip()


def above_space_recipe_limit(space):
    """
    Test if a space has reached its recipe limit
    :param space: Space to test for limits
    :return: Tuple (True if above or equal limit else false, message)
    """
    limit = space.max_recipes != 0 and Recipe.objects.filter(space=space).count() >= space.max_recipes
    if limit:
        return True, _('You have reached the maximum number of recipes for your space.')
    return False, ''


def above_space_user_limit(space):
    """
    Test if a space has reached its user limit
    :param space: Space to test for limits
    :return: Tuple (True if above or equal limit else false, message)
    """
    limit = space.max_users != 0 and UserSpace.objects.filter(space=space).count() > space.max_users
    if limit:
        return True, _('You have more users than allowed in your space.')
    return False, ''


def switch_user_active_space(user, space):
    """
    Switch the currently active space of a user by setting all spaces to inactive and activating the one passed
    :param user: user to change active space for
    :param space: space to activate user for
    :return user space object or none if not found/no permission
    """
    try:
        us = UserSpace.objects.get(space=space, user=user)
        if not us.active:
            UserSpace.objects.filter(user=user).update(active=False)
            us.active = True
            us.save()
            return us
        else:
            return us
    except ObjectDoesNotExist:
        return None
