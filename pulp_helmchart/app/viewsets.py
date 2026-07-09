from django.db import transaction
from django_filters import CharFilter
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from pulpcore.plugin.actions import ModifyRepositoryActionMixin
from pulpcore.plugin.serializers import AsyncOperationResponseSerializer
from pulpcore.plugin.tasking import dispatch
from pulpcore.plugin.viewsets import (
    ContentFilter,
    DistributionViewSet,
    OperationPostponedResponse,
    PublicationViewSet,
    RepositoryVersionViewSet,
    RepositoryViewSet,
    RolesMixin,
    SingleArtifactContentUploadViewSet,
)

from . import tasks
from .models import (
    HelmChartContent,
    HelmChartDistribution,
    HelmChartPublication,
    HelmChartRepository,
)
from .serializers import (
    HelmChartContentSerializer,
    HelmChartContentUploadSerializer,
    HelmChartDistributionSerializer,
    HelmChartPublicationSerializer,
    HelmChartRepositorySerializer,
)


class HelmChartContentFilter(ContentFilter):
    """
    FilterSet for HelmChartContent.
    """

    sha256 = CharFilter(field_name="digest")

    class Meta:
        model = HelmChartContent
        fields = ["name", "version", "filename", "sha256"]


class HelmChartContentViewSet(SingleArtifactContentUploadViewSet):
    """
    Packaged Helm chart archives that can be added to Helm chart repositories.
    """

    endpoint_name = "chart"
    queryset = HelmChartContent.objects.prefetch_related("_artifacts")
    serializer_class = HelmChartContentSerializer
    filterset_class = HelmChartContentFilter

    DEFAULT_ACCESS_POLICY = {
        "statements": [
            {
                "action": ["list", "retrieve"],
                "principal": "authenticated",
                "effect": "allow",
            },
            {
                "action": ["create"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": [
                    "has_required_repo_perms_on_upload:helmchart.modify_helmchartrepository",
                    "has_required_repo_perms_on_upload:helmchart.view_helmchartrepository",
                    "has_upload_param_model_or_domain_or_obj_perms:core.change_upload",
                ],
            },
            {
                "action": ["set_label", "unset_label"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": [
                    "has_model_or_domain_perms:core.manage_content_labels",
                ],
            },
            {
                "action": ["upload"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": [
                    "has_model_or_domain_perms:helmchart.upload_helmchart",
                ],
            },
        ],
        "queryset_scoping": {"function": "scope_queryset"},
    }

    LOCKED_ROLES = {
        "helmchart.helmchart_uploader": [
            "helmchart.upload_helmchart",
        ],
    }

    @extend_schema(
        description="Synchronously upload a packaged Helm chart.",
        request=HelmChartContentUploadSerializer,
        responses={201: HelmChartContentUploadSerializer},
        summary="Upload a packaged Helm chart.",
    )
    @action(detail=False, methods=["post"], serializer_class=HelmChartContentUploadSerializer)
    def upload(self, request):
        """Create a packaged Helm chart, optionally adding it to a repository."""
        serializer = self.get_serializer(data=request.data)
        with transaction.atomic():
            serializer.is_valid(raise_exception=True)
            serializer.save()

        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)


class HelmChartRepositoryViewSet(RepositoryViewSet, ModifyRepositoryActionMixin, RolesMixin):
    """
    Helm chart repository containing packaged chart archives.
    """

    endpoint_name = "helmchart"
    queryset = HelmChartRepository.objects.exclude(user_hidden=True)
    serializer_class = HelmChartRepositorySerializer
    queryset_filtering_required_permission = "helmchart.view_helmchartrepository"

    DEFAULT_ACCESS_POLICY = {
        "statements": [
            {
                "action": ["list", "my_permissions"],
                "principal": "authenticated",
                "effect": "allow",
            },
            {
                "action": ["create"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": [
                    "has_model_or_domain_perms:helmchart.add_helmchartrepository",
                ],
            },
            {
                "action": ["retrieve"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": "has_model_or_domain_or_obj_perms:helmchart.view_helmchartrepository",
            },
            {
                "action": ["destroy"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": [
                    "has_model_or_domain_or_obj_perms:helmchart.delete_helmchartrepository",
                    "has_model_or_domain_or_obj_perms:helmchart.view_helmchartrepository",
                ],
            },
            {
                "action": ["update", "partial_update", "set_label", "unset_label"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": [
                    "has_model_or_domain_or_obj_perms:helmchart.change_helmchartrepository",
                    "has_model_or_domain_or_obj_perms:helmchart.view_helmchartrepository",
                ],
            },
            {
                "action": ["modify"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": [
                    "has_model_or_domain_or_obj_perms:helmchart.modify_helmchartrepository",
                    "has_model_or_domain_or_obj_perms:helmchart.view_helmchartrepository",
                ],
            },
            {
                "action": ["list_roles", "add_role", "remove_role"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": [
                    "has_model_or_domain_or_obj_perms:"
                    "helmchart.manage_roles_helmchartrepository"
                ],
            },
        ],
        "creation_hooks": [
            {
                "function": "add_roles_for_object_creator",
                "parameters": {"roles": "helmchart.helmchartrepository_owner"},
            },
        ],
        "queryset_scoping": {"function": "scope_queryset"},
    }
    LOCKED_ROLES = {
        "helmchart.helmchartrepository_creator": [
            "helmchart.add_helmchartrepository",
        ],
        "helmchart.helmchartrepository_owner": [
            "helmchart.view_helmchartrepository",
            "helmchart.change_helmchartrepository",
            "helmchart.delete_helmchartrepository",
            "helmchart.modify_helmchartrepository",
            "helmchart.manage_roles_helmchartrepository",
            "helmchart.repair_helmchartrepository",
        ],
        "helmchart.helmchartrepository_viewer": ["helmchart.view_helmchartrepository"],
    }


class HelmChartRepositoryVersionViewSet(RepositoryVersionViewSet):
    """
    Repository versions for Helm chart repositories.
    """

    parent_viewset = HelmChartRepositoryViewSet

    DEFAULT_ACCESS_POLICY = {
        "statements": [
            {
                "action": ["list", "retrieve"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": (
                    "has_repository_model_or_domain_or_obj_perms:"
                    "helmchart.view_helmchartrepository"
                ),
            },
            {
                "action": ["destroy"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": [
                    "has_repository_model_or_domain_or_obj_perms:"
                    "helmchart.delete_helmchartrepository",
                    "has_repository_model_or_domain_or_obj_perms:"
                    "helmchart.view_helmchartrepository",
                ],
            },
            {
                "action": ["repair"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": [
                    "has_repository_model_or_domain_or_obj_perms:"
                    "helmchart.repair_helmchartrepository",
                    "has_repository_model_or_domain_or_obj_perms:"
                    "helmchart.view_helmchartrepository",
                ],
            },
        ],
    }


class HelmChartPublicationViewSet(PublicationViewSet, RolesMixin):
    """
    Helm chart publication containing chart archives and generated ``index.yaml``.
    """

    endpoint_name = "helmchart"
    queryset = HelmChartPublication.objects.exclude(complete=False)
    serializer_class = HelmChartPublicationSerializer
    queryset_filtering_required_permission = "helmchart.view_helmchartpublication"

    DEFAULT_ACCESS_POLICY = {
        "statements": [
            {
                "action": ["list", "my_permissions"],
                "principal": "authenticated",
                "effect": "allow",
            },
            {
                "action": ["create"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": [
                    "has_model_or_domain_perms:helmchart.add_helmchartpublication",
                    "has_repo_or_repo_ver_param_model_or_domain_or_obj_perms:"
                    "helmchart.view_helmchartrepository",
                ],
            },
            {
                "action": ["retrieve"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": "has_model_or_domain_or_obj_perms:helmchart.view_helmchartpublication",
            },
            {
                "action": ["destroy"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": [
                    "has_model_or_domain_or_obj_perms:helmchart.delete_helmchartpublication",
                    "has_model_or_domain_or_obj_perms:helmchart.view_helmchartpublication",
                ],
            },
            {
                "action": ["list_roles", "add_role", "remove_role"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": [
                    "has_model_or_domain_or_obj_perms:"
                    "helmchart.manage_roles_helmchartpublication"
                ],
            },
        ],
        "creation_hooks": [
            {
                "function": "add_roles_for_object_creator",
                "parameters": {"roles": "helmchart.helmchartpublication_owner"},
            },
        ],
        "queryset_scoping": {"function": "scope_queryset"},
    }
    LOCKED_ROLES = {
        "helmchart.helmchartpublication_creator": [
            "helmchart.add_helmchartpublication",
        ],
        "helmchart.helmchartpublication_owner": [
            "helmchart.view_helmchartpublication",
            "helmchart.delete_helmchartpublication",
            "helmchart.manage_roles_helmchartpublication",
        ],
        "helmchart.helmchartpublication_viewer": ["helmchart.view_helmchartpublication"],
    }

    @extend_schema(
        description="Trigger an asynchronous task to publish Helm chart content.",
        responses={202: AsyncOperationResponseSerializer},
    )
    def create(self, request):
        """
        Publish a Helm chart repository.

        Either ``repository`` or ``repository_version`` may be provided, but not both.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        repository_version = serializer.validated_data.get("repository_version")
        index = serializer.validated_data.get("index", "index.yaml")
        checkpoint = serializer.validated_data.get("checkpoint")

        kwargs = {"repository_version_pk": str(repository_version.pk), "index": index}
        if checkpoint:
            kwargs["checkpoint"] = True
        result = dispatch(
            tasks.publish,
            shared_resources=[repository_version.repository],
            kwargs=kwargs,
        )
        return OperationPostponedResponse(result, request)


class HelmChartDistributionViewSet(DistributionViewSet, RolesMixin):
    """
    Distribution serving a Helm chart publication or repository.
    """

    endpoint_name = "helmchart"
    queryset = HelmChartDistribution.objects.all()
    serializer_class = HelmChartDistributionSerializer
    queryset_filtering_required_permission = "helmchart.view_helmchartdistribution"

    DEFAULT_ACCESS_POLICY = {
        "statements": [
            {
                "action": ["list", "my_permissions"],
                "principal": "authenticated",
                "effect": "allow",
            },
            {
                "action": ["create"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": [
                    "has_model_or_domain_perms:helmchart.add_helmchartdistribution",
                    "has_repo_or_repo_ver_param_model_or_domain_or_obj_perms:"
                    "helmchart.view_helmchartrepository",
                    "has_publication_param_model_or_domain_or_obj_perms:"
                    "helmchart.view_helmchartpublication",
                ],
            },
            {
                "action": ["retrieve"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": "has_model_or_domain_or_obj_perms:helmchart.view_helmchartdistribution",
            },
            {
                "action": ["update", "partial_update", "set_label", "unset_label"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": [
                    "has_model_or_domain_or_obj_perms:helmchart.change_helmchartdistribution",
                    "has_model_or_domain_or_obj_perms:helmchart.view_helmchartdistribution",
                    "has_repo_or_repo_ver_param_model_or_domain_or_obj_perms:"
                    "helmchart.view_helmchartrepository",
                    "has_publication_param_model_or_domain_or_obj_perms:"
                    "helmchart.view_helmchartpublication",
                ],
            },
            {
                "action": ["destroy"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": [
                    "has_model_or_domain_or_obj_perms:helmchart.delete_helmchartdistribution",
                    "has_model_or_domain_or_obj_perms:helmchart.view_helmchartdistribution",
                ],
            },
            {
                "action": ["list_roles", "add_role", "remove_role"],
                "principal": "authenticated",
                "effect": "allow",
                "condition": [
                    "has_model_or_domain_or_obj_perms:"
                    "helmchart.manage_roles_helmchartdistribution"
                ],
            },
        ],
        "creation_hooks": [
            {
                "function": "add_roles_for_object_creator",
                "parameters": {"roles": "helmchart.helmchartdistribution_owner"},
            },
        ],
        "queryset_scoping": {"function": "scope_queryset"},
    }
    LOCKED_ROLES = {
        "helmchart.helmchartdistribution_creator": [
            "helmchart.add_helmchartdistribution",
        ],
        "helmchart.helmchartdistribution_owner": [
            "helmchart.view_helmchartdistribution",
            "helmchart.change_helmchartdistribution",
            "helmchart.delete_helmchartdistribution",
            "helmchart.manage_roles_helmchartdistribution",
        ],
        "helmchart.helmchartdistribution_viewer": ["helmchart.view_helmchartdistribution"],
    }
