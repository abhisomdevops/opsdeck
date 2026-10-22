"""Registered integrations. To add one: write a module with an ``Integration``
subclass and append it here (see CONTRIBUTING in the README)."""

from .argocd import ArgoCD
from .artifactory import Artifactory
from .aws import AWS
from .confluence import Confluence
from .flux import Flux
from .jenkins import Jenkins
from .jira import Jira
from .kubernetes import Kubernetes
from .prisma import Prisma

ALL_INTEGRATIONS = [Kubernetes, Flux, ArgoCD, Jenkins, Artifactory, AWS, Prisma, Jira, Confluence]
