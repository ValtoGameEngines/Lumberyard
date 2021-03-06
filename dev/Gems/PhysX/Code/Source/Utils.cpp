/*
* All or portions of this file Copyright (c) Amazon.com, Inc. or its affiliates or
* its licensors.
*
* For complete copyright and license terms please see the LICENSE at the root of this
* distribution (the "License"). All use of this software is governed by the License,
* or, if provided, by the license below or the license accompanying this file. Do not
* remove or modify any license notices. This file is distributed on an "AS IS" BASIS,
* WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*
*/

#include <PhysX_precompiled.h>

#include <AzCore/EBus/Results.h>
#include <AzCore/Interface/Interface.h>
#include <AzCore/RTTI/BehaviorContext.h>
#include <AzCore/Serialization/Utils.h>
#include <AzCore/Component/TransformBus.h>
#include <AzFramework/Physics/ShapeConfiguration.h>

#include <PhysX/ColliderShapeBus.h>
#include <PhysX/SystemComponentBus.h>
#include <PhysX/MeshAsset.h>
#include <PhysX/Utils.h>
#include <Source/SystemComponent.h>
#include <Source/Collision.h>
#include <Source/Pipeline/MeshAssetHandler.h>
#include <Source/Shape.h>
#include <Source/StaticRigidBodyComponent.h>
#include <Source/TerrainComponent.h>
#include <Source/RigidBodyStatic.h>
#include <Source/Utils.h>
#include <PhysX/PhysXLocks.h>

namespace PhysX
{
    namespace Utils
    {
        physx::PxBase* CreateNativeMeshObjectFromCookedData(const AZStd::vector<AZ::u8>& cookedData, 
            Physics::CookedMeshShapeConfiguration::MeshType meshType)
        {
            // PxDefaultMemoryInputData only accepts a non-const U8* pointer however keeps it as const U8* inside.
            // Hence we do const_cast here but it's safe to assume the data won't be modifed.
            physx::PxDefaultMemoryInputData inpStream(
                const_cast<physx::PxU8*>(cookedData.data()),
                static_cast<physx::PxU32>(cookedData.size()));

            if (meshType == Physics::CookedMeshShapeConfiguration::MeshType::Convex)
            {
                return PxGetPhysics().createConvexMesh(inpStream);
            }
            else
            {
                return PxGetPhysics().createTriangleMesh(inpStream);
            }
        }

        bool CreatePxGeometryFromConfig(const Physics::ShapeConfiguration& shapeConfiguration, physx::PxGeometryHolder& pxGeometry)
        {
            if (!shapeConfiguration.m_scale.IsGreaterThan(AZ::Vector3::CreateZero()))
            {
                AZ_Error("PhysX Utils", false, "Negative or zero values are invalid for shape configuration scale values %s",
                    ToString(shapeConfiguration.m_scale).c_str());
                return false;
            }

            auto shapeType = shapeConfiguration.GetShapeType();

            switch (shapeType)
            {
            case Physics::ShapeType::Sphere:
            {
                const Physics::SphereShapeConfiguration& sphereConfig = static_cast<const Physics::SphereShapeConfiguration&>(shapeConfiguration);
                if (sphereConfig.m_radius <= 0.0f)
                {
                    AZ_Error("PhysX Utils", false, "Invalid radius value: %f", sphereConfig.m_radius);
                    return false;
                }
                pxGeometry.storeAny(physx::PxSphereGeometry(sphereConfig.m_radius * shapeConfiguration.m_scale.GetMaxElement()));
                break;
            }
            case Physics::ShapeType::Box:
            {
                const Physics::BoxShapeConfiguration& boxConfig = static_cast<const Physics::BoxShapeConfiguration&>(shapeConfiguration);
                if (!boxConfig.m_dimensions.IsGreaterThan(AZ::Vector3::CreateZero()))
                {
                    AZ_Error("PhysX Utils", false, "Negative or zero values are invalid for box dimensions %s",
                        ToString(boxConfig.m_dimensions).c_str());
                    return false;
                }
                pxGeometry.storeAny(physx::PxBoxGeometry(PxMathConvert(boxConfig.m_dimensions * 0.5f * shapeConfiguration.m_scale)));
                break;
            }
            case Physics::ShapeType::Capsule:
            {
                const Physics::CapsuleShapeConfiguration& capsuleConfig = static_cast<const Physics::CapsuleShapeConfiguration&>(shapeConfiguration);
                float height = capsuleConfig.m_height * capsuleConfig.m_scale.GetZ();
                float radius = capsuleConfig.m_radius * capsuleConfig.m_scale.GetX().GetMax(capsuleConfig.m_scale.GetY());

                if (height <= 0.0f || radius <= 0.0f)
                {
                    AZ_Error("PhysX Utils", false, "Negative or zero values are invalid for capsule dimensions (height: %f, radius: %f)",
                        capsuleConfig.m_height, capsuleConfig.m_radius);
                    return false;
                }

                float halfHeight = 0.5f * height - radius;
                if (halfHeight <= 0.0f)
                {
                    AZ_Warning("PhysX", halfHeight < 0.0f, "Height must exceed twice the radius in capsule configuration (height: %f, radius: %f)",
                        capsuleConfig.m_height, capsuleConfig.m_radius);
                    halfHeight = std::numeric_limits<float>::epsilon();
                }
                pxGeometry.storeAny(physx::PxCapsuleGeometry(radius, halfHeight));
                break;
            }
            case Physics::ShapeType::Native:
            {
                const Physics::NativeShapeConfiguration& nativeShapeConfig = static_cast<const Physics::NativeShapeConfiguration&>(shapeConfiguration);
                AZ::Vector3 scale = nativeShapeConfig.m_nativeShapeScale * nativeShapeConfig.m_scale;
                physx::PxBase* meshData = reinterpret_cast<physx::PxBase*>(nativeShapeConfig.m_nativeShapePtr);
                return MeshDataToPxGeometry(meshData, pxGeometry, scale);
            }
            case Physics::ShapeType::CookedMesh:
            {
                const Physics::CookedMeshShapeConfiguration& cookedMeshShapeConfig = 
                    static_cast<const Physics::CookedMeshShapeConfiguration&>(shapeConfiguration);

                physx::PxBase* nativeMeshObject = nullptr;

                // Use the cached mesh object if it is there, otherwise create one and save in the shape configuration
                if (cookedMeshShapeConfig.GetCachedNativeMesh())
                {
                    nativeMeshObject = static_cast<physx::PxBase*>(cookedMeshShapeConfig.GetCachedNativeMesh());
                }
                else
                {
                    nativeMeshObject = CreateNativeMeshObjectFromCookedData(
                        cookedMeshShapeConfig.GetCookedMeshData(),
                        cookedMeshShapeConfig.GetMeshType());

                    if (nativeMeshObject)
                    {
                        cookedMeshShapeConfig.SetCachedNativeMesh(nativeMeshObject);
                    }
                    else
                    {
                        AZ_Warning("PhysX Rigid Body", false,
                            "Unable to create a mesh object from the CookedMeshShapeConfiguration buffer. "
                            "Please check if the data was cooked correctly.");
                        return false;
                    }
                }

                return MeshDataToPxGeometry(nativeMeshObject, pxGeometry, cookedMeshShapeConfig.m_scale);
            }
            case Physics::ShapeType::PhysicsAsset:
            {
                AZ_Assert(false,
                    "CreatePxGeometryFromConfig: Cannot pass PhysicsAsset configuration since it is a collection of shapes. "
                    "Please iterate over m_colliderShapes in the asset and call this function for each of them.");
                return false;
            }
            default:
                AZ_Warning("PhysX Rigid Body", false, "Shape not supported in PhysX. Shape Type: %d", shapeType);
                return false;
            }

            return true;
        }

        physx::PxShape* CreatePxShapeFromConfig(const Physics::ColliderConfiguration& colliderConfiguration, const Physics::ShapeConfiguration& shapeConfiguration, Physics::CollisionGroup& assignedCollisionGroup)
        {
            AZStd::vector<physx::PxMaterial*> materials;
            MaterialManagerRequestsBus::Broadcast(&MaterialManagerRequestsBus::Events::GetPxMaterials, colliderConfiguration.m_materialSelection, materials);

            if (materials.empty())
            {
                AZStd::shared_ptr<Material> defaultMaterial = nullptr;
                MaterialManagerRequestsBus::BroadcastResult(defaultMaterial, &MaterialManagerRequestsBus::Events::GetDefaultMaterial);
                if (!defaultMaterial)
                {
                    AZ_Error("PhysX", false, "Material array can't be empty!");
                    return nullptr;
                }
                materials.push_back(defaultMaterial->GetPxMaterial());
            }

            physx::PxGeometryHolder pxGeomHolder;
            if (Utils::CreatePxGeometryFromConfig(shapeConfiguration, pxGeomHolder))
            {
                auto materialsCount = static_cast<physx::PxU16>(materials.size());

                physx::PxShape* shape = PxGetPhysics().createShape(pxGeomHolder.any(), materials.begin(), materialsCount, colliderConfiguration.m_isExclusive);

                if (shape)
                {
                    Physics::CollisionGroup collisionGroup;
                    Physics::CollisionRequestBus::BroadcastResult(collisionGroup, &Physics::CollisionRequests::GetCollisionGroupById, colliderConfiguration.m_collisionGroupId);

                    physx::PxFilterData filterData = PhysX::Collision::CreateFilterData(colliderConfiguration.m_collisionLayer, collisionGroup);
                    shape->setSimulationFilterData(filterData);
                    shape->setQueryFilterData(filterData);

                    // Do custom logic for specific shape types
                    if (pxGeomHolder.getType() == physx::PxGeometryType::eCAPSULE)
                    {
                        // PhysX capsules are oriented around x by default.
                        physx::PxQuat pxQuat(AZ::Constants::HalfPi, physx::PxVec3(0.0f, 1.0f, 0.0f));
                        shape->setLocalPose(physx::PxTransform(pxQuat));
                    }

                    if (colliderConfiguration.m_isTrigger)
                    {
                        shape->setFlag(physx::PxShapeFlag::eSIMULATION_SHAPE, false);
                        shape->setFlag(physx::PxShapeFlag::eTRIGGER_SHAPE, true);
                        shape->setFlag(physx::PxShapeFlag::eSCENE_QUERY_SHAPE, false);
                    }

                    physx::PxTransform pxShapeTransform = PxMathConvert(colliderConfiguration.m_position, colliderConfiguration.m_rotation);
                    shape->setLocalPose(pxShapeTransform * shape->getLocalPose());

                    assignedCollisionGroup = collisionGroup;
                    return shape;
                }
                else
                {
                    AZ_Error("PhysX Rigid Body", false, "Failed to create shape.");
                    return nullptr;
                }
            }
            return nullptr;
        }

        World* GetDefaultWorld()
        {
            AZStd::shared_ptr<Physics::World> world = nullptr;
            Physics::DefaultWorldBus::BroadcastResult(world, &Physics::DefaultWorldRequests::GetDefaultWorld);
            return static_cast<World*>(world.get());
        }

        AZStd::string ConvexCookingResultToString(physx::PxConvexMeshCookingResult::Enum convexCookingResultCode)
        {
            static const AZStd::string resultToString[] = { "eSUCCESS", "eZERO_AREA_TEST_FAILED", "ePOLYGONS_LIMIT_REACHED", "eFAILURE" };
            if (AZ_ARRAY_SIZE(resultToString) > convexCookingResultCode)
            {
                return resultToString[convexCookingResultCode];
            }
            else
            {
                AZ_Error("PhysX", false, "Unknown convex cooking result code: %i", convexCookingResultCode);
                return "";
            }
        }

        AZStd::string TriMeshCookingResultToString(physx::PxTriangleMeshCookingResult::Enum triangleCookingResultCode)
        {
            static const AZStd::string resultToString[] = { "eSUCCESS", "eLARGE_TRIANGLE", "eFAILURE" };
            if (AZ_ARRAY_SIZE(resultToString) > triangleCookingResultCode)
            {
                return resultToString[triangleCookingResultCode];
            }
            else
            {
                AZ_Error("PhysX", false, "Unknown trimesh cooking result code: %i", triangleCookingResultCode);
                return "";
            }
        }

        bool WriteCookedMeshToFile(const AZStd::string& filePath, const AZStd::vector<AZ::u8>& physxData,
            Physics::CookedMeshShapeConfiguration::MeshType meshType)
        {
            Pipeline::MeshAssetData assetData;

            AZStd::shared_ptr<Pipeline::AssetColliderConfiguration> colliderConfig;
            AZStd::shared_ptr<Physics::CookedMeshShapeConfiguration> shapeConfig = AZStd::make_shared<Physics::CookedMeshShapeConfiguration>();

            shapeConfig->SetCookedMeshData(physxData.data(), physxData.size(), meshType);

            assetData.m_colliderShapes.emplace_back(colliderConfig, shapeConfig);

            return Utils::WriteCookedMeshToFile(filePath, assetData);
        }

        bool WriteCookedMeshToFile(const AZStd::string& filePath, const Pipeline::MeshAssetData& assetData)
        {
            AZ::SerializeContext* serializeContext = nullptr;
            AZ::ComponentApplicationBus::BroadcastResult(serializeContext, &AZ::ComponentApplicationRequests::GetSerializeContext);
            return AZ::Utils::SaveObjectToFile(filePath, AZ::DataStream::ST_BINARY, &assetData, serializeContext);
        }

        bool CookConvexToPxOutputStream(const AZ::Vector3* vertices, AZ::u32 vertexCount, physx::PxOutputStream& stream)
        {
            physx::PxCooking* cooking = nullptr;
            SystemRequestsBus::BroadcastResult(cooking, &SystemRequests::GetCooking);

            physx::PxConvexMeshDesc convexDesc;
            convexDesc.points.count = vertexCount;
            convexDesc.points.stride = sizeof(AZ::Vector3);
            convexDesc.points.data = vertices;
            convexDesc.flags = physx::PxConvexFlag::eCOMPUTE_CONVEX;

            physx::PxConvexMeshCookingResult::Enum resultCode = physx::PxConvexMeshCookingResult::eSUCCESS;

            bool result = cooking->cookConvexMesh(convexDesc, stream, &resultCode);

            AZ_Error("PhysX", result,
                "CookConvexToPxOutputStream: Failed to cook convex mesh. Please check the data is correct. Error: %s",
                Utils::ConvexCookingResultToString(resultCode));

            return result;
        }

        bool CookTriangleMeshToToPxOutputStream(const AZ::Vector3* vertices, AZ::u32 vertexCount, 
            const AZ::u32* indices, AZ::u32 indexCount, physx::PxOutputStream& stream)
        {
            physx::PxCooking* cooking = nullptr;
            SystemRequestsBus::BroadcastResult(cooking, &SystemRequests::GetCooking);

            // Validate indices size
            AZ_Error("PhysX", indexCount % 3 == 0, "Number of indices must be a multiple of 3.");

            physx::PxTriangleMeshDesc meshDesc;
            meshDesc.points.count = vertexCount;
            meshDesc.points.stride = sizeof(AZ::Vector3);
            meshDesc.points.data = vertices;

            meshDesc.triangles.count = indexCount / 3;
            meshDesc.triangles.stride = sizeof(AZ::u32) * 3;
            meshDesc.triangles.data = indices;

            physx::PxTriangleMeshCookingResult::Enum resultCode = physx::PxTriangleMeshCookingResult::eSUCCESS;

            bool result = cooking->cookTriangleMesh(meshDesc, stream, &resultCode);

            AZ_Error("PhysX", result,
                "CookTriangleMeshToToPxOutputStream: Failed to cook triangle mesh. Please check the data is correct. Error: %s.",
                Utils::TriMeshCookingResultToString(resultCode));

            return result;
        }

        bool MeshDataToPxGeometry(physx::PxBase* meshData, physx::PxGeometryHolder& pxGeometry, const AZ::Vector3& scale)
        {
            if (meshData)
            {
                if (meshData->is<physx::PxTriangleMesh>())
                {
                    pxGeometry.storeAny(physx::PxTriangleMeshGeometry(reinterpret_cast<physx::PxTriangleMesh*>(meshData), physx::PxMeshScale(PxMathConvert(scale))));
                }
                else
                {
                    pxGeometry.storeAny(physx::PxConvexMeshGeometry(reinterpret_cast<physx::PxConvexMesh*>(meshData), physx::PxMeshScale(PxMathConvert(scale))));
                }

                return true;
            }
            else
            {
                AZ_Error("PhysXUtils::MeshDataToPxGeometry", false, "Mesh data is null.");
                return false;
            }
        }

        bool ReadFile(const AZStd::string& path, AZStd::vector<uint8_t>& buffer)
        {
            AZ::IO::FileIOBase* fileIO = AZ::IO::FileIOBase::GetInstance();
            if (!fileIO)
            {
                AZ_Warning("PhysXUtils::ReadFile", false, "No File System");
                return false;
            }

            // Open file
            AZ::IO::HandleType file;
            if (!fileIO->Open(path.c_str(), AZ::IO::OpenMode::ModeRead, file))
            {
                AZ_Warning("PhysXUtils::ReadFile", false, "Failed to open file:%s", path.c_str());
                return false;
            }

            // Get file size, we want to read the whole thing in one go
            AZ::u64 fileSize;
            if (!fileIO->Size(file, fileSize))
            {
                AZ_Warning("PhysXUtils::ReadFile", false, "Failed to read file size:%s", path.c_str());
                fileIO->Close(file);
                return false;
            }

            if (fileSize <= 0)
            {
                AZ_Warning("PhysXUtils::ReadFile", false, "File is empty:%s", path.c_str());
                fileIO->Close(file);
                return false;
            }

            buffer.resize(fileSize);

            AZ::u64 bytesRead = 0;
            bool failOnFewerThanSizeBytesRead = false;
            if (!fileIO->Read(file, &buffer[0], fileSize, failOnFewerThanSizeBytesRead, &bytesRead))
            {
                AZ_Warning("PhysXUtils::ReadFile", false, "Failed to read file:%s", path.c_str());
                fileIO->Close(file);
                return false;
            }

            fileIO->Close(file);

            return true;
        }

        void GetMaterialList(
            AZStd::vector<physx::PxMaterial*>& pxMaterials, const AZStd::vector<int>& terrainSurfaceIdIndexMapping,
            const Physics::TerrainMaterialSurfaceIdMap& terrainMaterialsToSurfaceIds)
        {
            pxMaterials.reserve(terrainSurfaceIdIndexMapping.size());

            AZStd::shared_ptr<Material> defaultMaterial;
            MaterialManagerRequestsBus::BroadcastResult(defaultMaterial, &MaterialManagerRequestsBus::Events::GetDefaultMaterial);

            if (terrainSurfaceIdIndexMapping.empty())
            {
                pxMaterials.push_back(defaultMaterial->GetPxMaterial());
                return;
            }

            AZStd::vector<physx::PxMaterial*> materials;

            for (auto& surfaceId : terrainSurfaceIdIndexMapping)
            {
                const auto& userAssignedMaterials = terrainMaterialsToSurfaceIds;
                const auto& matSelectionIterator = userAssignedMaterials.find(surfaceId);
                if (matSelectionIterator != userAssignedMaterials.end())
                {
                    MaterialManagerRequestsBus::Broadcast(&MaterialManagerRequests::GetPxMaterials, matSelectionIterator->second, materials);

                    if (!materials.empty())
                    {
                        pxMaterials.push_back(materials.front());
                    }
                    else
                    {
                        AZ_Error("PhysX", false, "Creating materials: array with materials can't be empty");
                        pxMaterials.push_back(defaultMaterial->GetPxMaterial());
                    }
                }
                else
                {
                    pxMaterials.push_back(defaultMaterial->GetPxMaterial());
                }
            }
        }

        AZStd::unique_ptr<Physics::RigidBodyStatic> CreateTerrain(
            const PhysX::TerrainConfiguration& configuration, const AZ::EntityId& entityId, const AZStd::string_view& name)
        {
            if (!configuration.m_heightFieldAsset.IsReady())
            {
                AZ_Warning("PhysXUtils::CreateTerrain", false, "Heightfield asset not ready");
                return nullptr;
            }

            physx::PxHeightField* heightField = configuration.m_heightFieldAsset.Get()->GetHeightField();
            if (!heightField)
            {
                AZ_Warning("PhysXUtils::CreateTerrain", false, "HeightField Asset has no heightfield");
                return nullptr;
            }

            // Get terrain materials
            AZStd::vector<physx::PxMaterial*> materialList;
            GetMaterialList(materialList, configuration.m_terrainSurfaceIdIndexMapping, configuration.m_terrainMaterialsToSurfaceIds);

            const float heightScale = configuration.m_scale.GetZ();
            const float rowScale = configuration.m_scale.GetX();
            const float colScale = configuration.m_scale.GetY();

            physx::PxHeightFieldGeometry heightfieldGeom(heightField, physx::PxMeshGeometryFlags(), heightScale, rowScale, colScale);
            AZ_Warning("Terrain Component", heightfieldGeom.isValid(), "Invalid height field");

            if (!heightfieldGeom.isValid())
            {
                AZ_Warning("Terrain Component", false, "Invalid height field");
                return nullptr;
            }

            physx::PxShape* pxShape = PxGetPhysics().createShape(heightfieldGeom, materialList.begin(), static_cast<physx::PxU16>(materialList.size()), true);
            physx::PxQuat rotateZ(physx::PxHalfPi, physx::PxVec3(0.0f, 0.0f, 1.0f));
            physx::PxQuat rotateX(physx::PxHalfPi, physx::PxVec3(1.0f, 0.0f, 0.0f));
            pxShape->setLocalPose(physx::PxTransform(rotateZ * rotateX));

            AZStd::shared_ptr<PhysX::Shape> heightFieldShape = AZStd::make_shared<PhysX::Shape>(pxShape);
            pxShape->release();

            Physics::CollisionLayer terrainCollisionLayer = configuration.m_collisionLayer;
            Physics::CollisionGroup terrainCollisionGroup = Physics::CollisionGroup::All;
            Physics::CollisionRequestBus::BroadcastResult(terrainCollisionGroup, &Physics::CollisionRequests::GetCollisionGroupById, configuration.m_collisionGroup);

            heightFieldShape->SetCollisionLayer(terrainCollisionLayer);
            heightFieldShape->SetCollisionGroup(terrainCollisionGroup);
            heightFieldShape->SetName(name.data());

            Physics::WorldBodyConfiguration staticRigidBodyConfiguration;
            staticRigidBodyConfiguration.m_position = AZ::Vector3::CreateZero();
            staticRigidBodyConfiguration.m_entityId = entityId;
            staticRigidBodyConfiguration.m_debugName = name;

            AZStd::unique_ptr<Physics::RigidBodyStatic> terrainTile = AZStd::make_unique<PhysX::RigidBodyStatic>(staticRigidBodyConfiguration);
            terrainTile->AddShape(heightFieldShape);

            return terrainTile;
        }

        AZStd::string ReplaceAll(AZStd::string str, const AZStd::string& fromString, const AZStd::string& toString) {
            size_t positionBegin = 0;
            while ((positionBegin = str.find(fromString, positionBegin)) != AZStd::string::npos) 
            {
                str.replace(positionBegin, fromString.length(), toString);
                positionBegin += toString.length();
            }
            return str;
        }

        void WarnEntityNames(const AZStd::vector<AZ::EntityId>& entityIds, const char* category, const char* message)
        {
            AZStd::string messageOutput = message;
            messageOutput += "\n";
            for (const auto& entityId : entityIds)
            {
                AZ::Entity* entity = nullptr;
                AZ::ComponentApplicationBus::BroadcastResult(entity, &AZ::ComponentApplicationRequests::FindEntity, entityId);
                if (entity)
                {
                    messageOutput += entity->GetName() + "\n";
                }
            }

            AZStd::string percentageSymbol("%");
            AZStd::string percentageReplace("%%"); //Replacing % with %% serves to escape the % character when printing out the entity names in printf style.
            messageOutput = ReplaceAll(messageOutput, percentageSymbol, percentageReplace);

            AZ_Warning(category, false, messageOutput.c_str());
        }

        AZ::Transform GetColliderLocalTransform(const AZ::Vector3& colliderRelativePosition,
            const AZ::Quaternion& colliderRelativeRotation)
        {
            return AZ::Transform::CreateFromQuaternionAndTranslation(colliderRelativeRotation, colliderRelativePosition);
        }

        AZ::Transform GetColliderWorldTransform(const AZ::Transform& worldTransform,
            const AZ::Vector3& colliderRelativePosition,
            const AZ::Quaternion& colliderRelativeRotation)
        {
            return worldTransform * GetColliderLocalTransform(colliderRelativePosition, colliderRelativeRotation);
        }

        void ColliderPointsLocalToWorld(AZStd::vector<AZ::Vector3>& pointsInOut,
            const AZ::Transform& worldTransform,
            const AZ::Vector3& colliderRelativePosition,
            const AZ::Quaternion& colliderRelativeRotation)
        {
            AZ::Transform transform = GetColliderWorldTransform(worldTransform,
                colliderRelativePosition,
                colliderRelativeRotation);

            for (AZ::Vector3& point : pointsInOut)
            {
                point = transform * point;
            }
        }

        AZ::Aabb GetPxGeometryAabb(const physx::PxGeometryHolder& geometryHolder,
            const AZ::Transform& worldTransform,
            const ::Physics::ColliderConfiguration& colliderConfiguration
            )
        {
            const float boundsInflationFactor = 1.0f;
            const physx::PxBounds3 bounds = physx::PxGeometryQuery::getWorldBounds(geometryHolder.any(),
                    physx::PxTransform(
                        PxMathConvert(PhysX::Utils::GetColliderWorldTransform(worldTransform,
                        colliderConfiguration.m_position,
                        colliderConfiguration.m_rotation))),
                    boundsInflationFactor);
            return PxMathConvert(bounds);
        }

        AZ::Aabb GetColliderAabb(const AZ::Transform& worldTransform,
            const ::Physics::ShapeConfiguration& shapeConfiguration,
            const ::Physics::ColliderConfiguration& colliderConfiguration)
        {
            const AZ::Aabb worldPosAabb = AZ::Aabb::CreateFromPoint(worldTransform.GetPosition());
            physx::PxGeometryHolder geometryHolder;
            bool isAssetShape = shapeConfiguration.GetShapeType() == Physics::ShapeType::PhysicsAsset;

            if (!isAssetShape)
            {
                if (CreatePxGeometryFromConfig(shapeConfiguration, geometryHolder))
                {
                    return GetPxGeometryAabb(geometryHolder, worldTransform, colliderConfiguration);
                }
                return worldPosAabb;
            }
            else
            {
                const Physics::PhysicsAssetShapeConfiguration& physicsAssetConfig =
                    static_cast<const Physics::PhysicsAssetShapeConfiguration&>(shapeConfiguration);

                if (!physicsAssetConfig.m_asset.IsReady())
                {
                    return worldPosAabb;
                }

                Physics::ShapeConfigurationList colliderShapes;
                GetColliderShapeConfigsFromAsset(physicsAssetConfig,
                    colliderConfiguration,
                    colliderShapes);

                if (colliderShapes.empty())
                {
                    return worldPosAabb;
                }

                AZ::Aabb aabb = AZ::Aabb::CreateNull();
                for (const auto& colliderShape : colliderShapes)
                {
                    if (colliderShape.second &&
                        CreatePxGeometryFromConfig(*colliderShape.second, geometryHolder))
                    {
                        aabb.AddAabb(
                            GetPxGeometryAabb(geometryHolder, worldTransform, colliderConfiguration)
                        );
                    }
                    else
                    {
                        return worldPosAabb;
                    }
                }
                return aabb;
            }
        }

        bool TriggerColliderExists(AZ::EntityId entityId)
        {
            AZ::EBusLogicalResult<bool, AZStd::logical_or<bool>> response(false);
            PhysX::ColliderShapeRequestBus::EventResult(response,
                entityId,
                &PhysX::ColliderShapeRequestBus::Events::IsTrigger);
            return response.value;
        }

        void GetColliderShapeConfigsFromAsset(const Physics::PhysicsAssetShapeConfiguration& assetConfiguration,
            const Physics::ColliderConfiguration& masterColliderConfiguration,
            Physics::ShapeConfigurationList& resultingColliderShapes)
        {
            if (!assetConfiguration.m_asset.IsReady())
            {
                AZ_Error("PhysX", false, "GetColliderShapesFromAsset: Asset %s is not ready."
                    "Please make sure the calling code connects to the AssetBus and "
                    "creates the collider shapes only when OnAssetReady or OnAssetReload is invoked.",
                    assetConfiguration.m_asset.GetHint().c_str());
                return;
            }

            const Pipeline::MeshAsset* asset = assetConfiguration.m_asset.GetAs<Pipeline::MeshAsset>();

            if (!asset)
            {
                AZ_Error("PhysX", false, "GetColliderShapesFromAsset: Mesh Asset %s is null."
                    "Please check the file is in the correct format. Try to delete it and get AssetProcessor re-create it. "
                    "The data is loaded in Pipeline::MeshAssetHandler::LoadAssetData()",
                    assetConfiguration.m_asset.GetHint().c_str());
                return;
            }

            const Pipeline::MeshAssetData& assetData = asset->m_assetData;
            const Pipeline::MeshAssetData::ShapeConfigurationList& shapeConfigList = assetData.m_colliderShapes;
            
            resultingColliderShapes.reserve(resultingColliderShapes.size() + shapeConfigList.size());

            for (size_t shapeIndex = 0; shapeIndex < shapeConfigList.size(); shapeIndex++)
            {
                const Pipeline::MeshAssetData::ShapeConfigurationPair& shapeConfigPair = shapeConfigList[shapeIndex];

                AZStd::shared_ptr<Physics::ColliderConfiguration> thisColliderConfiguration = 
                    AZStd::make_shared<Physics::ColliderConfiguration>(masterColliderConfiguration);

                AZ::u16 shapeMaterialIndex = assetData.m_materialIndexPerShape[shapeIndex];

                // Triangle meshes have material indices cooked in the data.
                if(shapeMaterialIndex != Pipeline::MeshAssetData::TriangleMeshMaterialIndex)
                {
                    // Clear the materials that came in from the component collider configuration
                    thisColliderConfiguration->m_materialSelection.SetMaterialSlots({});
                
                    // Set the material that is relevant for this specific shape
                    Physics::MaterialId assignedMaterialForShape =
                        masterColliderConfiguration.m_materialSelection.GetMaterialId(shapeMaterialIndex);
                    thisColliderConfiguration->m_materialSelection.SetMaterialId(assignedMaterialForShape);
                }

                // Here we use the collider configuration data saved in the asset to update the one coming from the component
                if (const Pipeline::AssetColliderConfiguration* optionalColliderData = shapeConfigPair.first.get())
                {
                    optionalColliderData->UpdateColliderConfiguration(*thisColliderConfiguration);
                }

                // Update the scale with the data from the asset configuration
                AZStd::shared_ptr<Physics::ShapeConfiguration> thisShapeConfiguration = shapeConfigPair.second;
                thisShapeConfiguration->m_scale = assetConfiguration.m_scale * assetConfiguration.m_assetScale;

                resultingColliderShapes.emplace_back(thisColliderConfiguration, thisShapeConfiguration);
            }
        }

        void GetShapesFromAsset(const Physics::PhysicsAssetShapeConfiguration& assetConfiguration,
            const Physics::ColliderConfiguration& masterColliderConfiguration,
            AZStd::vector<AZStd::shared_ptr<Physics::Shape>>& resultingShapes)
        {
            Physics::ShapeConfigurationList resultingColliderShapeConfigs;
            GetColliderShapeConfigsFromAsset(assetConfiguration, masterColliderConfiguration, resultingColliderShapeConfigs);

            resultingShapes.reserve(resultingShapes.size() + resultingColliderShapeConfigs.size());

            for (const Physics::ShapeConfigurationPair& shapeConfigPair : resultingColliderShapeConfigs)
            {
                // Scale the collider offset
                shapeConfigPair.first->m_position *= shapeConfigPair.second->m_scale;

                AZStd::shared_ptr<Physics::Shape> shape;
                Physics::SystemRequestBus::BroadcastResult(shape, &Physics::SystemRequests::CreateShape, 
                    *shapeConfigPair.first, *shapeConfigPair.second);

                if (shape)
                {
                    resultingShapes.emplace_back(shape);
                }
            }
        }

        AZ::Vector3 GetNonUniformScale(AZ::EntityId entityId)
        {
            AZ::Vector3 worldScale = AZ::Vector3::CreateOne();
            AZ::TransformBus::EventResult(worldScale, entityId, &AZ::TransformBus::Events::GetWorldScale);
            return worldScale;
        }

        AZ::Vector3 GetUniformScale(AZ::EntityId entityId)
        {
            const float uniformScale = GetNonUniformScale(entityId).GetMaxElement();
            return AZ::Vector3(uniformScale);
        }

        namespace Geometry
        {
            PointList GenerateBoxPoints(const AZ::Vector3& min, const AZ::Vector3& max)
            {
                PointList pointList;

                auto size = max - min;

                const auto minSamples = 2.f;
                const auto maxSamples = 8.f;
                const auto desiredSampleDelta = 2.f;

                // How many sample in each axis
                int numSamples[] =
                {
                    static_cast<int>((size.GetX() / desiredSampleDelta).GetClamp(minSamples, maxSamples)),
                    static_cast<int>((size.GetY() / desiredSampleDelta).GetClamp(minSamples, maxSamples)),
                    static_cast<int>((size.GetZ() / desiredSampleDelta).GetClamp(minSamples, maxSamples))
                };

                float sampleDelta[] =
                {
                    size.GetX() / static_cast<float>(numSamples[0] - 1),
                    size.GetY() / static_cast<float>(numSamples[1] - 1),
                    size.GetZ() / static_cast<float>(numSamples[2] - 1),
                };

                for (auto i = 0; i < numSamples[0]; ++i)
                {
                    for (auto j = 0; j < numSamples[1]; ++j)
                    {
                        for (auto k = 0; k < numSamples[2]; ++k)
                        {
                            pointList.emplace_back(
                                min.GetX() + i * sampleDelta[0],
                                min.GetY() + j * sampleDelta[1],
                                min.GetZ() + k * sampleDelta[2]
                            );
                        }
                    }
                }

                return pointList;
            }

            PointList GenerateSpherePoints(float radius)
            {
                PointList points;

                int nSamples = static_cast<int>(radius * 5);
                nSamples = AZ::GetClamp(nSamples, 5, 512);

                // Draw arrows using Fibonacci sphere
                float offset = 2.f / nSamples;
                float increment = AZ::Constants::Pi * (3.f - sqrt(5.f));
                for (int i = 0; i < nSamples; ++i)
                {
                    float phi = ((i + 1) % nSamples) * increment;
                    float y = ((i * offset) - 1) + (offset / 2.f);
                    float r = sqrt(1 - pow(y, 2));
                    float x = cos(phi) * r;
                    float z = sin(phi) * r;
                    points.emplace_back(x * radius, y * radius, z * radius);
                }
                return points;
            }

            PointList GenerateCylinderPoints(float height, float radius)
            {
                PointList points;
                AZ::Vector3 base(0.f, 0.f, -height * 0.5f);
                AZ::Vector3 radiusVector(radius, 0.f, 0.f);

                const auto sides = AZ::GetClamp(radius, 3.f, 8.f);
                const auto segments = AZ::GetClamp(height * 0.5f, 2.f, 8.f);
                const auto angleDelta = AZ::Quaternion::CreateRotationZ(AZ::Constants::TwoPi / sides);
                const auto segmentDelta = height / (segments - 1);
                for (auto segment = 0; segment < segments; ++segment)
                {
                    for (auto side = 0; side < sides; ++side)
                    {
                        auto point = base + radiusVector;
                        points.emplace_back(point);
                        radiusVector = angleDelta * radiusVector;
                    }
                    base += AZ::Vector3(0, 0, segmentDelta);
                }
                return points;
            }
        } // namespace Geometry
    } // namespace Utils

    namespace ReflectionUtils
    {
        // Forwards invokation of CalculateNetForce in a force region to script canvas.
        class ForceRegionBusBehaviorHandler
            : public ForceRegionNotificationBus::Handler
            , public AZ::BehaviorEBusHandler
        {
        public:
            AZ_EBUS_BEHAVIOR_BINDER(ForceRegionBusBehaviorHandler, "{EB6C0F7A-0BDA-4052-84C0-33C05E3FF739}", AZ::SystemAllocator
                , OnCalculateNetForce
            );

            static void Reflect(AZ::ReflectContext* context);

            /// Callback invoked when net force exerted on object is computed by a force region.
            void OnCalculateNetForce(AZ::EntityId forceRegionEntityId
                , AZ::EntityId targetEntityId
                , const AZ::Vector3& netForceDirection
                , float netForceMagnitude) override;
        };

        void ReflectPhysXOnlyApi(AZ::ReflectContext* context)
        {
            ForceRegionBusBehaviorHandler::Reflect(context);
        }

        void ForceRegionBusBehaviorHandler::Reflect(AZ::ReflectContext* context)
        {
            if (AZ::BehaviorContext* behaviorContext = azrtti_cast<AZ::BehaviorContext*>(context))
            {
                behaviorContext->EBus<PhysX::ForceRegionNotificationBus>("ForceRegionNotificationBus")
                    ->Attribute(AZ::Script::Attributes::Module, "physics")
                    ->Attribute(AZ::Script::Attributes::Scope, AZ::Script::Attributes::ScopeFlags::Common)
                    ->Handler<ForceRegionBusBehaviorHandler>()
                    ;
            }
        }

        void ForceRegionBusBehaviorHandler::OnCalculateNetForce(AZ::EntityId forceRegionEntityId
            , AZ::EntityId targetEntityId
            , const AZ::Vector3& netForceDirection
            , float netForceMagnitude)
        {
            Call(FN_OnCalculateNetForce
                , forceRegionEntityId
                , targetEntityId
                , netForceDirection
                , netForceMagnitude);
        }
    } // namespace ReflectionUtils

    namespace PxActorFactories
    {
        physx::PxRigidDynamic* CreatePxRigidBody(const Physics::RigidBodyConfiguration& configuration)
        {
            physx::PxTransform pxTransform(PxMathConvert(configuration.m_position),
                PxMathConvert(configuration.m_orientation).getNormalized());

            physx::PxRigidDynamic* rigidDynamic = PxGetPhysics().createRigidDynamic(pxTransform);

            if (!rigidDynamic)
            {
                AZ_Error("PhysX Rigid Body", false, "Failed to create PhysX rigid actor. Name: %s", configuration.m_debugName.c_str());
                return nullptr;
            }

            rigidDynamic->setMass(configuration.m_mass);
            rigidDynamic->setSleepThreshold(configuration.m_sleepMinEnergy);
            rigidDynamic->setLinearVelocity(PxMathConvert(configuration.m_initialLinearVelocity));
            rigidDynamic->setAngularVelocity(PxMathConvert(configuration.m_initialAngularVelocity));
            rigidDynamic->setLinearDamping(configuration.m_linearDamping);
            rigidDynamic->setAngularDamping(configuration.m_angularDamping);
            rigidDynamic->setCMassLocalPose(physx::PxTransform(PxMathConvert(configuration.m_centerOfMassOffset)));
            rigidDynamic->setRigidBodyFlag(physx::PxRigidBodyFlag::eKINEMATIC, configuration.m_kinematic);
            rigidDynamic->setMaxAngularVelocity(configuration.m_maxAngularVelocity);
            return rigidDynamic;
        }

        physx::PxRigidStatic* CreatePxStaticRigidBody(const Physics::WorldBodyConfiguration& configuration)
        {
            physx::PxTransform pxTransform(PxMathConvert(configuration.m_position),
                PxMathConvert(configuration.m_orientation).getNormalized());
            physx::PxRigidStatic* rigidStatic = PxGetPhysics().createRigidStatic(pxTransform);
            return rigidStatic;
        }

        void ReleaseActor(physx::PxActor* actor)
        {
            if (!actor)
            {
                return;
            }

            physx::PxScene* scene = actor->getScene();
            if (scene)
            {
                PHYSX_SCENE_WRITE_LOCK(scene);
                scene->removeActor(*actor);
            }

            if (auto userData = Utils::GetUserData(actor))
            {
                userData->Invalidate();
            }

            actor->release();
        }
    } // namespace PxActorFactories

    namespace StaticRigidBodyUtils
    {
        bool EntityHasComponentsUsingService(const AZ::Entity& entity, AZ::Crc32 service)
        {
            const AZ::Entity::ComponentArrayType& components = entity.GetComponents();

            return AZStd::any_of(components.begin(), components.end(),
                [service](const AZ::Component* component) -> bool
            {
                AZ::ComponentDescriptor* componentDescriptor = nullptr;
                AZ::ComponentDescriptorBus::EventResult(
                    componentDescriptor, azrtti_typeid(component), &AZ::ComponentDescriptorBus::Events::GetDescriptor);

                AZ::ComponentDescriptor::DependencyArrayType services;
                componentDescriptor->GetDependentServices(services, nullptr);

                return AZStd::find(services.begin(), services.end(), service) != services.end();
            }
            );
        }

        bool CanCreateRuntimeComponent(const AZ::Entity& editorEntity)
        {
            // Allow to create runtime StaticRigidBodyComponent if there are no components
            // using 'PhysXColliderService' attached to entity.
            const AZ::Crc32 physxColliderServiceId = AZ_CRC("PhysXColliderService", 0x4ff43f7c);

            return !EntityHasComponentsUsingService(editorEntity, physxColliderServiceId);
        }

        bool TryCreateRuntimeComponent(const AZ::Entity& editorEntity, AZ::Entity& gameEntity)
        {
            // Only allow single StaticRigidBodyComponent per entity
            const auto* staticRigidBody = gameEntity.FindComponent<StaticRigidBodyComponent>();
            if (staticRigidBody)
            {
                return false;
            }

            if (CanCreateRuntimeComponent(editorEntity))
            {
                gameEntity.CreateComponent<StaticRigidBodyComponent>();
                return true;
            }

            return false;
        }
    } // namespace StaticRigidBodyUtils
} // namespace PhysX
