#
# does some work
# 
import sys
import os
import azlmbr
import azlmbrtest

def print_entity_id(entityId):
    print ('entity_id {} {}'.format(entityId.id, entityId.isValid()))
