#!/usr/bin/env python3
"""Validate complete published image data; never confer signing or deploy authority."""
import copy
import re

try:
    from . import image_build, registry_policy, release_manifest as records
except ImportError:
    import image_build
    import registry_policy
    import release_manifest as records

LOCAL_KEYS = {'workload','environment','platform','manifest_digest','config_digest',
              'archive_sha256','archive_size','source_commit','recipe_sha256',
              'build_inputs_sha256','published','deployable'}
IMAGE_KEYS = LOCAL_KEYS | {'image','registry_content_verified','visibility_verified',
                          'attested','trust_verified','package_id'}
RECEIPT_KEYS = {'schema_version','artifact_kind','controller','source_manifest_sha256',
                'build_receipt_sha256','images','external_images','admission_evidence_sha256',
                'published','registry_content_verified','visibility_verified','attested',
                'trust_verified','deployable','receipt_sha256'}
DIGEST = re.compile(r'sha256:[a-f0-9]{64}')


class PublicationError(ValueError):
    """Rejected data and provider errors never enter diagnostics."""


def require(value):
    if not value:
        raise PublicationError('Publication record refused')


def qualify(*, receipt, manifest, public, controller):
    """Cross-check a hash-bound publisher receipt against trusted build inputs.

    Acquisition must separately bind native artifact ID/digest/run metadata.
    Cryptographic attestation verification and fresh deployment admission remain
    required; input completion flags cannot grant either capability.
    """
    try:
        receipt,manifest,public,controller = copy.deepcopy((receipt,manifest,public,controller))
        records.bounded_tree(receipt)
        tasks = image_build.build_tasks(manifest,public,controller,'release')
        require(controller['run_attempt']==1 and len(tasks)==15)
        require(isinstance(receipt,dict) and set(receipt)==RECEIPT_KEYS
                and type(receipt['schema_version']) is int and receipt['schema_version']==1
                and receipt['artifact_kind']=='content-verified-image-publications'
                and receipt['controller']==controller
                and receipt['source_manifest_sha256']==records.canonical_sha256(manifest)
                and receipt['external_images']==manifest['external_images'])
        require(all(receipt[key] is True for key in ('published','registry_content_verified','visibility_verified'))
                and all(receipt[key] is False for key in ('attested','trust_verified','deployable')))
        require(receipt['receipt_sha256']==records.canonical_sha256(
            {key:value for key,value in receipt.items() if key!='receipt_sha256'}))
        evidence = receipt['admission_evidence_sha256']
        require(isinstance(evidence,list) and len(evidence)==15)
        for digest in evidence:records.hash_value(digest)
        images = receipt['images'];require(isinstance(images,list) and len(images)==15)
        local, packages, ids = [],{},set()
        for task,image in zip(tasks,images):
            require(isinstance(image,dict) and set(image)==IMAGE_KEYS
                    and all(image[key]==task[key] for key in
                            ('workload','environment','platform','source_commit','recipe_sha256','build_inputs_sha256')))
            require(all(image[key] is True for key in ('published','registry_content_verified','visibility_verified'))
                    and all(image[key] is False for key in ('attested','trust_verified','deployable')))
            for key in ('manifest_digest','config_digest'):
                require(isinstance(image[key],str) and DIGEST.fullmatch(image[key]))
            records.hash_value(image['archive_sha256'])
            require(type(image['archive_size']) is int and 0<image['archive_size']<=image_build.MAX_ARCHIVE
                    and type(image['package_id']) is int and 0<image['package_id']<=9007199254740991)
            require(image['image']==registry_policy.EXPECTED['registry_prefix']+'/'+task['workload']+'@'+image['manifest_digest'])
            workload = task['workload']; package_id = image['package_id']
            if workload in packages:
                require(packages[workload]==package_id)
            else:
                require(package_id not in ids);ids.add(package_id);packages[workload]=package_id
            local.append({**{key:image[key] for key in LOCAL_KEYS},'published':False,'deployable':False})
        build = {'schema_version':1,'artifact_kind':'local-oci-build-receipts','controller':controller,
                 'source_manifest_sha256':records.canonical_sha256(manifest),'images':local,
                 'external_images':manifest['external_images'],'published':False,'deployable':False}
        require(receipt['build_receipt_sha256']==records.canonical_sha256(build))
        return receipt
    except Exception:
        raise PublicationError('Publication record refused') from None
