# -*- coding: utf-8 -*-
#
# Copyright (c) 2012-2023 Virtual Cable S.L.U.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
#    * Redistributions of source code must retain the above copyright notice,
#      this list of conditions and the following disclaimer.
#    * Redistributions in binary form must reproduce the above copyright notice,
#      this list of conditions and the following disclaimer in the documentation
#      and/or other materials provided with the distribution.
#    * Neither the name of Virtual Cable S.L.U. nor the names of its contributors
#      may be used to endorse or promote products derived from this software
#      without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""
Author: Adolfo Gómez, dkmaster at dkmon dot com
"""
import logging
import typing

from django.utils.translation import gettext as _

from uds import models
from uds.core import types

# from uds.models.meta_pool import MetaPool, MetaPoolMember
# from uds.models.service_pool import ServicePool
# from uds.models.user_service import UserService
# from uds.models.user import User

from uds.core.types.states import State
from uds.core.util.model import process_uuid
from uds.core.util import log, ensure
from uds.REST.model import DetailHandler
from .user_services import AssignedUserService, UserServiceItem

if typing.TYPE_CHECKING:
    from django.db.models import Model

logger = logging.getLogger(__name__)

class MetaItem(types.rest.ItemDictType):
    """
    Item type for a Meta Pool Member
    """
    id: str
    pool_id: str
    pool_name: typing.NotRequired[str]  # Optional, as it can be not present
    name: str
    comments: str
    priority: int
    enabled: bool
    user_services_count: int
    user_services_in_preparation: int

class MetaServicesPool(DetailHandler[MetaItem]):
    """
    Processes the transports detail requests of a Service Pool
    """
    

    @staticmethod
    def as_dict(item: models.MetaPoolMember) -> 'MetaItem':
        return {
            'id': item.uuid,
            'pool_id': item.pool.uuid,
            'name': item.pool.name,
            'comments': item.pool.comments,
            'priority': item.priority,
            'enabled': item.enabled,
            'user_services_count': item.pool.userServices.exclude(state__in=State.INFO_STATES).count(),
            'user_services_in_preparation': item.pool.userServices.filter(state=State.PREPARING).count(),
        }

    def get_items(self, parent: 'Model', item: typing.Optional[str]) -> types.rest.GetItemsResult['MetaItem']:
        parent = ensure.is_instance(parent, models.MetaPool)
        try:
            if not item:
                return [MetaServicesPool.as_dict(i) for i in parent.members.all()]
            i = parent.members.get(uuid=process_uuid(item))
            return MetaServicesPool.as_dict(i)
        except Exception:
            logger.exception('err: %s', item)
            raise self.invalid_item_response()

    def get_title(self, parent: 'Model') -> str:
        return _('Service pools')

    def get_fields(self, parent: 'Model') -> list[typing.Any]:
        return [
            {'priority': {'title': _('Priority'), 'type': 'numeric', 'width': '6em'}},
            {'name': {'title': _('Service Pool name')}},
            {'enabled': {'title': _('Enabled')}},
        ]

    def save_item(self, parent: 'Model', item: typing.Optional[str]) -> typing.Any:
        parent = ensure.is_instance(parent, models.MetaPool)
        # If already exists
        uuid = process_uuid(item) if item else None

        pool = models.ServicePool.objects.get(uuid=process_uuid(self._params['pool_id']))
        enabled = self._params['enabled'] not in ('false', False, '0', 0)
        priority = int(self._params['priority'])
        priority = priority if priority >= 0 else 0

        if uuid is not None:
            member = parent.members.get(uuid=uuid)
            member.pool = pool 
            member.enabled = enabled
            member.priority = priority
            member.save()
        else:
            member = parent.members.create(pool=pool, priority=priority, enabled=enabled)
            
        log.log(
            parent,
            types.log.LogLevel.INFO,
            ("Added" if uuid is None else "Modified")
            + " meta pool member {}/{}/{} by {}".format(pool.name, priority, enabled, self._user.pretty_name),
            types.log.LogSource.ADMIN,
        )

        return {'id': member.uuid}


    def delete_item(self, parent: 'Model', item: str) -> None:
        parent = ensure.is_instance(parent, models.MetaPool)
        member = parent.members.get(uuid=process_uuid(self._args[0]))
        log_str = "Removed meta pool member {} by {}".format(member.pool.name, self._user.pretty_name)

        member.delete()

        log.log(parent, types.log.LogLevel.INFO, log_str, types.log.LogSource.ADMIN)


class MetaAssignedService(DetailHandler[UserServiceItem]):
    """
    Rest handler for Assigned Services, wich parent is Service
    """

    @staticmethod
    def item_as_dict(
        meta_pool: 'models.MetaPool',
        item: 'models.UserService',
        props: typing.Optional[dict[str, typing.Any]],
    ) -> 'UserServiceItem':
        element = AssignedUserService.item_as_dict(item, props, False)
        element['pool_id'] = item.deployed_service.uuid
        element['pool_name'] = item.deployed_service.name
        return element

    def _get_assigned_userservice(self, metapool: models.MetaPool, userservice_id: str) -> models.UserService:
        """
        Gets an assigned service and checks that it belongs to this metapool
        If not found, raises InvalidItemException
        """
        try:
            return models.UserService.objects.filter(
                uuid=process_uuid(userservice_id),
                cache_level=0,
                deployed_service__in=[i.pool for i in metapool.members.all()],
            )[0]
        except Exception:
            raise self.invalid_item_response()

    def get_items(self, parent: 'Model', item: typing.Optional[str]) -> types.rest.GetItemsResult[UserServiceItem]:
        parent = ensure.is_instance(parent, models.MetaPool)
        def _assigned_userservices_for_pools() -> (
            typing.Generator[
                tuple[models.UserService, typing.Optional[dict[str, typing.Any]]], None, None
            ]
        ):
            for m in parent.members.filter(enabled=True):
                properties: dict[str, typing.Any] = {
                    k: v
                    for k, v in models.Properties.objects.filter(
                        owner_type='userservice',
                        owner_id__in=m.pool.assigned_user_services().values_list('uuid', flat=True),
                    ).values_list('key', 'value')
                }
                for u in (
                    m.pool.assigned_user_services()
                    .filter(state__in=State.VALID_STATES)
                    .prefetch_related('deployed_service', 'publication')
                ):
                    yield u, properties.get(u.uuid, {})

        try:
            if not item:  # All items
                result: dict[str, typing.Any] = {}

                for k, props in _assigned_userservices_for_pools():
                    result[k.uuid] = MetaAssignedService.item_as_dict(parent, k, props)
                return list(result.values())

            return MetaAssignedService.item_as_dict(
                parent,
                self._get_assigned_userservice(parent, item),
                props={
                    k: v
                    for k, v in models.Properties.objects.filter(
                        owner_type='userservice', owner_id=process_uuid(item)
                    ).values_list('key', 'value')
                },
            )
        except Exception:
            logger.exception('get_items')
            raise self.invalid_item_response()

    def get_title(self, parent: 'Model') -> str:
        parent = ensure.is_instance(parent, models.MetaPool)
        return _('Assigned services')

    def get_fields(self, parent: 'Model') -> list[typing.Any]:
        parent = ensure.is_instance(parent, models.MetaPool)
        return [
            {'creation_date': {'title': _('Creation date'), 'type': 'datetime'}},
            {'pool_name': {'title': _('Pool')}},
            {'unique_id': {'title': 'Unique ID'}},
            {'ip': {'title': _('IP')}},
            {'friendly_name': {'title': _('Friendly name')}},
            {
                'state': {
                    'title': _('status'),
                    'type': 'dict',
                    'dict': State.literals_dict(),
                }
            },
            {'in_use': {'title': _('In Use')}},
            {'source_host': {'title': _('Src Host')}},
            {'source_ip': {'title': _('Src Ip')}},
            {'owner': {'title': _('Owner')}},
            {'actor_version': {'title': _('Actor version')}},
        ]

    def get_row_style(self, parent: 'Model') -> types.ui.RowStyleInfo:
        return types.ui.RowStyleInfo(prefix='row-state-', field='state')
        
    def get_logs(self, parent: 'Model', item: str) -> list[typing.Any]:
        parent = ensure.is_instance(parent, models.MetaPool)
        try:
            asigned_userservice = self._get_assigned_userservice(parent, item)
            logger.debug('Getting logs for %s', asigned_userservice)
            return log.get_logs(asigned_userservice)
        except Exception:
            raise self.invalid_item_response()

    def delete_item(self, parent: 'Model', item: str) -> None:
        parent = ensure.is_instance(parent, models.MetaPool)
        userservice = self._get_assigned_userservice(parent, item)

        if userservice.user:
            log_str = 'Deleted assigned service {} to user {} by {}'.format(
                userservice.friendly_name,
                userservice.user.pretty_name,
                self._user.pretty_name,
            )
        else:
            log_str = 'Deleted cached service {} by {}'.format(userservice.friendly_name, self._user.pretty_name)

        if userservice.state in (State.USABLE, State.REMOVING):
            userservice.release()
        elif userservice.state == State.PREPARING:
            userservice.cancel()
        elif userservice.state == State.REMOVABLE:
            raise self.invalid_item_response(_('Item already being removed'))
        else:
            raise self.invalid_item_response(_('Item is not removable'))

        log.log(parent, types.log.LogLevel.INFO, log_str, types.log.LogSource.ADMIN)

    # Only owner is allowed to change right now
    def save_item(self, parent: 'Model', item: typing.Optional[str]) -> typing.Any:
        parent = ensure.is_instance(parent, models.MetaPool)
        if item is None:
            raise self.invalid_item_response()

        fields = self.fields_from_params(['auth_id', 'user_id'])
        userservice = self._get_assigned_userservice(parent, item)
        user = models.User.objects.get(uuid=process_uuid(fields['user_id']))

        log_str = 'Changing ownership of service from {} to {} by {}'.format(
            userservice.user.pretty_name if userservice.user else 'unknown', user.pretty_name, self._user.pretty_name
        )

        # If there is another service that has this same owner, raise an exception
        if (
            userservice.deployed_service.userServices.filter(user=user)
            .exclude(uuid=userservice.uuid)
            .exclude(state__in=State.INFO_STATES)
            .count()
            > 0
        ):
            raise self.invalid_response_response(
                'There is already another user service assigned to {}'.format(user.pretty_name)
            )

        userservice.user = user
        userservice.save()

        # Log change
        log.log(parent, types.log.LogLevel.INFO, log_str, types.log.LogSource.ADMIN)
        
        return {'id': userservice.uuid}
